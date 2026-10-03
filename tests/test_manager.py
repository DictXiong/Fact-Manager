import hashlib
import json
from pathlib import Path
import time

import pytest

from fact_manager.core import Store
from fact_manager.server import Authentication, COOKIE, COOKIE_MAX_AGE
from fact_manager.workflow import PROMPT, export_review
from fact_manager.legacy_prompt import LEGACY_PROMPT


def add_candidate(catalog, lid, value="12"):
    sid = catalog.add_text(lid, "记录", "实验温度为12度。")
    store = catalog.store(lid)
    chunk = store.chunks(sid)[0]
    store.add_candidates(
        sid,
        chunk["id"],
        [
            {
                "entity": "实验",
                "attribute": "温度",
                "value": value,
                "unit": "度",
                "conditions": "",
                "valid_from": "",
                "valid_until": "",
                "quote": "实验温度为12度。",
            }
        ],
    )
    row = store.review_rows()[0]
    change = {
        **row,
        "decision": "approve",
        "external_use": "yes",
        "file_sha256": row["file_hash"],
    }
    return sid, row, change


def rpc(client, token, tool, arguments=None, rid=1):
    response = client.post(
        "/mcp",
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/json, text/event-stream",
        },
        json={
            "jsonrpc": "2.0",
            "id": rid,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments or {}},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["result"]


def test_admin_session_csrf_and_mcp_auth_are_separate(catalog, web):
    lid = catalog.create("研究记录")["id"]
    token = catalog.issue_token(lid, "助手")["token"]
    assert (
        web.post(
            "/mcp", headers={"Authorization": "Bearer " + "a" * 64}, json={}
        ).status_code
        == 401
    )
    web.cookies.clear()
    assert (
        web.get(
            "/api/libraries", headers={"Authorization": "Bearer " + token}
        ).status_code
        == 401
    )
    response = web.post("/api/login", json={"token": "a" * 64})
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=strict" in cookie
    assert (
        web.post(
            "/api/libraries", headers={"X-Fact-CSRF": ""}, json={"name": "无防护"}
        ).status_code
        == 403
    )
    assert (
        web.post(
            "/api/libraries",
            headers={"Origin": "https://attacker.test"},
            json={"name": "跨站"},
        ).status_code
        == 403
    )


def test_library_isolation_token_revocation_and_no_secret_storage(catalog, web):
    a = catalog.create("实验记录")["id"]
    b = catalog.create("其他主题")["id"]
    sid, row, change = add_candidate(catalog, a)
    store = catalog.store(a)
    store.review([change], "审核人")
    store.publish("发布人")
    token = catalog.issue_token(a, "助手")
    other = catalog.issue_token(b, "其他助手")
    assert "12" in json.dumps(
        rpc(web, token["token"], "get_verified_facts"), ensure_ascii=False
    )
    assert not rpc(web, other["token"], "get_verified_facts").get("isError", False)
    assert (
        rpc(web, other["token"], "get_verified_facts")
        .get("structuredContent", {})
        .get("result", [])
        == []
    )
    assert rpc(
        web,
        other["token"],
        "read_source",
        {"source_id": sid, "chunk_id": row["chunk_id"]},
    ).get("isError")
    assert (
        rpc(
            web,
            token["token"],
            "read_source",
            {"source_id": sid, "chunk_id": row["chunk_id"]},
        ).get("isError", False)
        is False
    )
    assert (
        web.post(
            "/api/libraries/" + b + "/review",
            json={"reviewer": "误操作", "changes": [change]},
        ).status_code
        == 400
    )
    with catalog.connect() as db:
        saved = db.execute(
            "SELECT token_hash FROM tokens WHERE id=?", (token["id"],)
        ).fetchone()[0]
        assert saved == hashlib.sha256(token["token"].encode()).hexdigest()
    listed = web.get("/api/libraries/" + a + "/tokens").json()
    assert token["token"] not in json.dumps(listed)
    catalog.revoke_token(a, token["id"])
    assert (
        web.post(
            "/mcp", headers={"Authorization": "Bearer " + token["token"]}, json={}
        ).status_code
        == 401
    )


def test_mcp_rechecks_scope_for_alternating_requests(catalog, web):
    a = catalog.create("A")["id"]
    b = catalog.create("B")["id"]
    at = catalog.issue_token(a, "A")["token"]
    bt = catalog.issue_token(b, "B")["token"]
    for i, t in enumerate([at, bt, at, bt]):
        result = rpc(web, t, "get_library", rid=i + 1)
        data = json.loads(result["content"][0]["text"])
        assert data["id"] == (a if i % 2 == 0 else b)


def test_web_review_evidence_checks_publish_and_excel(catalog, web):
    lib = web.post(
        "/api/libraries", json={"name": "实验", "description": "记录条件"}
    ).json()["id"]
    sid, row, change = add_candidate(catalog, lib)
    base = "/api/libraries/" + lib
    assert web.get(base + "/facts?status=pending").json()["total"] == 1
    tampered = {**change, "quote": "伪造的引用"}
    assert (
        web.post(
            base + "/review", json={"reviewer": "审核人", "changes": [tampered]}
        ).status_code
        == 400
    )
    response = web.get(base + "/review.xlsx")
    assert response.status_code == 200 and response.content.startswith(b"PK")
    assert (
        web.post(
            base + "/review", json={"reviewer": "审核人", "changes": [change]}
        ).status_code
        == 200
    )
    assert catalog.store(lib).verified() == []
    assert (
        web.post(base + "/publish", json={"reviewer": "发布人"}).json()["published"]
        == 1
    )
    assert len(catalog.store(lib).verified()) == 1
    assert len(web.get(base + "/facts/" + row["id"] + "/audit").json()) == 2
    assert (
        web.post(
            base + "/facts/" + row["id"] + "/retire",
            json={"reviewer": "审核人", "reason": "已过时"},
        ).status_code
        == 200
    )
    assert catalog.store(lib).verified() == []


def test_local_source_update_and_scope_removal_hide_facts(catalog, web):
    lid = catalog.create("读书笔记")["id"]
    sid, row, change = add_candidate(catalog, lid)
    store = catalog.store(lid)
    store.review([change], "审核")
    store.publish("发布")
    doc = store.source(sid)["document_id"]
    catalog.add_text(lid, "新版本", "实验温度为15度。", doc)
    assert catalog.store(lid).verified() == []
    assert (
        web.get("/api/libraries/" + lid + "/sources/" + sid + "/original").status_code
        == 400
    )
    latest = catalog.store(lid).sources()[0]
    catalog.disable_source(lid, latest["id"])
    assert catalog.store(lid).sources() == []


def test_multiple_tokens_expiry_and_wrong_library_revoke(catalog, web):
    lid = catalog.create("主题")["id"]
    other = catalog.create("其他主题")["id"]
    tokens = [catalog.issue_token(lid, n) for n in ["助手A", "助手B"]]
    assert len(catalog.tokens(lid)) == 2
    assert not catalog.revoke_token(other, tokens[0]["id"])
    assert catalog.authenticate_mcp(tokens[0]["token"]) == lid
    with catalog.connect() as db:
        db.execute(
            "UPDATE tokens SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (tokens[0]["id"],),
        )
    assert catalog.authenticate_mcp(tokens[0]["token"]) is None
    assert catalog.authenticate_mcp(tokens[1]["token"]) == lid


def test_safe_text_upload_and_xss_remains_data(catalog, web):
    lid = catalog.create("<script>alert(1)</script>")["id"]
    base = "/api/libraries/" + lid
    assert (
        web.post(
            base + "/sources/upload",
            files={
                "file": (
                    "记录.md",
                    "事实：<script>alert(1)</script>".encode(),
                    "text/markdown",
                )
            },
        ).status_code
        == 201
    )
    assert (
        web.post(
            base + "/sources/upload",
            files={"file": ("不可解析.pdf", b"%PDF-1", "application/pdf")},
        ).status_code
        == 400
    )
    assert web.get("/").status_code == 200
    assert "script-src 'self'" in web.get("/").headers["content-security-policy"]
    assert (
        "innerHTML"
        not in Path(__file__)
        .parents[1]
        .joinpath("fact_manager/static/app.js")
        .read_text()
    )


def test_jobs_local_sync_does_not_call_model(catalog, web):
    lid = catalog.create("本地记录")["id"]
    catalog.add_text(lid, "文本", "温度12度。")
    base = "/api/libraries/" + lid
    assert web.post(base + "/jobs", json={"kind": "sync"}).status_code == 202
    for _ in range(100):
        jobs = web.get(base + "/jobs").json()
        if jobs[0]["status"] not in ("queued", "running"):
            break
        time.sleep(0.01)
    assert jobs[0]["status"] == "succeeded"
    assert catalog.store(lid).review_rows() == []


def test_legacy_migration_preserves_ids_workbook_and_extraction(tmp_path, catalog):
    old = Store(tmp_path / "legacy", ["dataset"])
    sid = old.register(
        "dataset", "doc", "旧材料", b"file", [{"id": "chunk", "content": "温度12度。"}]
    )
    old.add_candidates(
        sid,
        "chunk",
        [
            {
                "entity": "实验",
                "attribute": "温度",
                "value": "12",
                "unit": "度",
                "conditions": "",
                "valid_from": "",
                "valid_until": "",
                "quote": "温度12度。",
            }
        ],
        extraction=(0, "deepseek-flash", "harnets-kb-facts-1"),
    )
    workbook = old.root / "reviews" / "pending.xlsx"
    export_review(old, workbook)
    library = catalog.migrate_legacy(old.root, ["dataset"])
    assert (
        library["prompt"] == LEGACY_PROMPT
        and library["prompt_version"] == "harnets-kb-facts-1"
    )
    migrated = catalog.store("harnets")
    assert migrated.review_rows()[0]["id"] == old.review_rows()[0]["id"]
    assert (
        migrated.review_rows()[0]["fingerprint"] == old.review_rows()[0]["fingerprint"]
    )
    assert migrated.extracted(sid, "chunk", 0, "deepseek-flash", "harnets-kb-facts-1")
    assert Path(migrated.source(sid)["original_path"]).read_bytes() == b"file"
    assert (
        migrated.root / "reviews" / "pending.xlsx"
    ).read_bytes() == workbook.read_bytes()
    with pytest.raises(ValueError, match="already exists"):
        catalog.migrate_legacy(old.root, ["dataset"])


def test_prompt_versions_only_change_when_prompt_changes(catalog):
    l = catalog.create("主题")
    updated = catalog.update(l["id"], "新名称", "说明", l["prompt"])
    assert updated["prompt_version"] == l["prompt_version"]
    updated = catalog.update(l["id"], "新名称", "说明", PROMPT + "\n只提取技术指标。")
    assert updated["prompt_version"] != l["prompt_version"]


def test_persistent_session_survives_time_and_restart_but_not_token_rotation(
    monkeypatch,
):
    auth = Authentication(None, None, "a" * 64, "facts.example.test")
    value, csrf = auth.issue_session()
    cookie = COOKIE + "=" + value
    monkeypatch.setattr("fact_manager.server.time.time", lambda: 4102444800)
    restarted = Authentication(None, None, "a" * 64, "facts.example.test")
    session = restarted.session(cookie)
    assert session is not None and session["expires"] is None
    assert restarted.csrf(session) == csrf
    assert Authentication(None, None, "b" * 64, "facts.example.test").session(cookie) is None
    assert restarted.session(cookie[:-1] + ("0" if cookie[-1] != "0" else "1")) is None


def test_cookie_renews_on_use_and_logout_cannot_renew_it(web):
    from http.cookies import SimpleCookie

    response = web.post("/api/login", json={"token": "a" * 64})
    web.headers["X-Fact-CSRF"] = response.json()["csrf"]
    cookies = SimpleCookie()
    cookies.load(response.headers["set-cookie"])
    initial = cookies[COOKIE].value
    assert cookies[COOKIE]["max-age"] == str(COOKIE_MAX_AGE)
    assert cookies[COOKIE]["secure"] and cookies[COOKIE]["httponly"]
    response = web.get("/api/session")
    assert (
        response.status_code == 200
        and response.json()["csrf"] == web.headers["X-Fact-CSRF"]
    )
    cookies = SimpleCookie()
    cookies.load(response.headers["set-cookie"])
    assert cookies[COOKIE].value == initial and cookies[COOKIE]["max-age"] == str(
        COOKIE_MAX_AGE
    )
    response = web.post("/api/logout", json={})
    assert response.status_code == 200
    headers = response.headers.get_list("set-cookie")
    assert len(headers) == 1
    cookies = SimpleCookie()
    cookies.load(headers[0])
    assert cookies[COOKIE]["max-age"] == "0"
    assert web.get("/api/session").status_code == 401


def test_live_legacy_session_upgrades_without_changing_csrf(web):
    from http.cookies import SimpleCookie

    auth = Authentication(None, None, "a" * 64, "facts.example.test")
    legacy = {"expires": int(time.time()) + 3600, "nonce": "legacy-session-nonce"}
    web.cookies.clear()
    web.cookies.set(COOKIE, auth._sign_session(legacy), domain="facts.example.test", path="/")
    response = web.get("/api/session")
    assert response.status_code == 200 and response.json()["csrf"] == auth.csrf(legacy)
    cookies = SimpleCookie()
    cookies.load(response.headers["set-cookie"])
    assert auth.session(COOKIE + "=" + cookies[COOKIE].value)["expires"] is None
    web.headers["X-Fact-CSRF"] = response.json()["csrf"]
    assert (
        web.post("/api/libraries", json={"name": "Renewed session"}).status_code == 201
    )


def test_expired_legacy_session_cannot_become_permanent(web):
    auth = Authentication(None, None, "a" * 64, "facts.example.test")
    legacy = {"expires": int(time.time()) - 1, "nonce": "expired-session-nonce"}
    web.cookies.clear()
    web.cookies.set(COOKIE, auth._sign_session(legacy), domain="facts.example.test", path="/")
    response = web.get("/api/session")
    assert response.status_code == 401 and "set-cookie" not in response.headers
