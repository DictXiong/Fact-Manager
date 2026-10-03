import hashlib
import io
import json
from pathlib import Path
import sqlite3
import zipfile

import httpx
from pypdf import PdfWriter
import pytest

from fact_manager.catalog import Catalog
from fact_manager.checking import check_with_model, local_check, review_packet
from fact_manager.core import FIELDS, Store
from fact_manager.evidence import inspect
from fact_manager.reasoning import approved_revision
from test_manager import rpc


def candidate(value="2/3", attribute="交换机数量比例", **kwargs):
    return dict(
        entity="测试A",
        attribute=attribute,
        value=value,
        unit="",
        conditions="同一规模，与Clos对比",
        valid_from="",
        valid_until="",
        quote="测试A交换机数量为Clos的2/3。",
        **kwargs
    )


def setup(store):
    sid = store.register(
        "local",
        "d",
        "test.txt",
        "测试A交换机数量为Clos的2/3。".encode(),
        [{"id": "c", "content": "测试A交换机数量为Clos的2/3。"}],
    )
    store.add_candidates(sid, "c", [candidate()])
    return store.fact(store.review_rows()[0]["id"])


def approve(store, fact, external=True, changes=None):
    store.review(
        [
            {
                **fact,
                **(changes or {}),
                "file_sha256": fact["file_hash"],
                "external_use": "yes" if external else "no",
                "decision": "approve",
            }
        ],
        "测试员",
    )
    return store.fact(fact["id"])


def derive(store, p, rule="ratio_complement", candidate=None):
    return store.propose_derived(
        [p["id"]],
        "在同一规模与Clos基线下计算，不涉及成本",
        rule=rule,
        candidate=candidate,
        premise_revisions={p["id"]: p["revision"]},
    )


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "facts", ["local", "dataset"])


def test_check_is_advisory_and_revision_guarded(store):
    f = setup(store)
    check = store.save_check(
        f["id"],
        "needs_review",
        "检查基线",
        "agent",
        expected_revision=f["revision"],
        suggestions={"conditions": "待核对基线"},
    )
    assert check["current"]
    assert store.fact(f["id"])["conditions"] == f["conditions"]
    assert store.statistics()["pending"] == 1
    assert store.verified(for_external=False) == []
    with pytest.raises(ValueError, match="target changed"):
        store.save_check(
            f["id"], "supported", "支持", "agent", expected_revision="stale"
        )
    approve(store, f, changes={"value": "0.66"})
    assert not store.fact(f["id"])["check"]["current"]


def test_contradiction_requires_current_approved_premises(store):
    p = approve(store, setup(store))
    store.add_candidates(p["source_id"], "c", [candidate(value="1/2")])
    f = store.fact(store.review_rows()[0]["id"])
    with pytest.raises(ValueError, match="must cite"):
        store.save_check(
            f["id"], "contradicted", "不同", "agent", expected_revision=f["revision"]
        )
    with pytest.raises(ValueError, match="Premise changed"):
        store.save_check(
            f["id"],
            "contradicted",
            "不同",
            "agent",
            expected_revision=f["revision"],
            premise_ids=[p["id"]],
            premise_revisions={p["id"]: "old"},
        )
    store.save_check(
        f["id"],
        "contradicted",
        "同场景不同值",
        "agent",
        expected_revision=f["revision"],
        premise_ids=[p["id"]],
        premise_revisions={p["id"]: p["revision"]},
    )
    store.retire(p["id"], "管理员", "原值有误")
    assert not store.fact(f["id"])["check"]["current"]


def test_approved_context_change_invalidates_model_check(store):
    f = setup(store)
    with store.connect() as db:
        context = approved_revision(store, db)
    store.save_check(
        f["id"],
        "supported",
        "直接证据",
        "model",
        kind="ai",
        expected_revision=f["revision"],
        context_revision=context,
    )
    assert store.fact(f["id"])["check"]["current"]
    approve(store, f)
    assert not store.fact(f["id"])["check"]["current"]


def test_strict_derivation_pending_and_transitive_invalidation(store):
    p = approve(store, setup(store))
    d = derive(store, p)
    assert d["value"] == "1/3" and d["attribute"] == "交换机数量补比例"
    assert d["status"] == "pending" and not d["external_use"] and d["origin"]["current"]
    assert store.verified() == []
    d = approve(store, d)
    child = derive(
        store,
        d,
        rule="reasoned",
        candidate={
            **{k: d[k] for k in FIELDS},
            "attribute": "约束内减少量说明",
            "value": "减少三分之一",
        },
    )
    approve(store, child)
    assert store.publish("管理员") == 3
    assert len(store.verified()) == 3
    store.retire(p["id"], "管理员", "前提纠正")
    assert store.verified() == []
    assert not store.fact(d["id"])["origin"]["current"]
    assert not store.fact(child["id"])["origin"]["current"]
    assert store.fact(child["id"])["status"] == "published"


def test_derived_conclusion_cannot_be_edited_or_externalize_private_premise(store):
    p = approve(store, setup(store), external=False)
    d = derive(store, p)
    with pytest.raises(ValueError, match="Do not edit"):
        approve(store, d, changes={"value": "0.3"}, external=False)
    with pytest.raises(ValueError, match="external use"):
        approve(store, d, external=True)
    approve(store, d, external=False)
    store.publish("管理员")
    assert store.verified() == [] and len(store.verified(for_external=False)) == 2


def test_pending_premises_and_changed_source_block_derivation(store):
    p = setup(store)
    with pytest.raises(ValueError, match="approved"):
        derive(store, p)
    p = approve(store, p)
    d = derive(store, p)
    store.register(
        "local", "d", "test.txt", b"new source", [{"id": "new", "content": "新版本"}]
    )
    assert store.approved_facts() == []
    assert not store.fact(d["id"])["origin"]["current"]
    with pytest.raises(ValueError, match="stale"):
        approve(store, d)


def test_derivation_premise_revision_and_expiry_guards(store):
    p = approve(store, setup(store))
    with pytest.raises(ValueError, match="Premise changed"):
        store.propose_derived(
            [p["id"]],
            "旧前提",
            rule="ratio_complement",
            premise_revisions={p["id"]: "stale"},
        )
    d = approve(store, derive(store, p))
    store.publish("管理员")
    with store.connect() as db:
        db.execute("UPDATE facts SET valid_until='2000-01-01' WHERE id=?", (p["id"],))
    assert store.verified() == [] and not store.fact(d["id"])["origin"]["current"]


def test_qualified_ratio_rejected_and_no_division_error(store):
    p = approve(store, setup(store), changes={"value": "约2/3"})
    with pytest.raises(ValueError, match="Exact ratio"):
        derive(store, p)
    with store.connect() as db:
        db.execute("UPDATE facts SET value='1/0' WHERE id=?", (p["id"],))
    p = store.fact(p["id"])
    with pytest.raises(ValueError, match="Invalid ratio"):
        derive(store, p)


def test_original_integrity_and_parsed_snapshot_are_verified(store):
    f = setup(store)
    e = inspect(store, f["source_id"], "c")
    assert e["integrity"] == "verified" and "交换机数量" in e["native"]["text"]
    original = Path(store.source(f["source_id"])["original_path"])
    original.write_text("tampered")
    with pytest.raises(ValueError, match="SHA256"):
        inspect(store, f["source_id"], "c")
    with pytest.raises(ValueError, match="SHA256"):
        approve(store, f)


def test_corrupted_chunks_block_packet(store):
    f = setup(store)
    with store.connect() as db:
        db.execute("UPDATE chunks SET content='corrupt'")
    with pytest.raises(ValueError, match="snapshot hash"):
        review_packet(store, [f["id"]])


def test_missing_original_hides_published_facts(store):
    f = approve(store, setup(store))
    store.publish("管理员")
    Path(store.source(f["source_id"])["original_path"]).unlink()
    assert store.verified() == []


def test_word_native_table_and_empty_pdf_warning(store):
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as z:
        z.writestr(
            "word/document.xml",
            """<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:tbl><w:tr><w:tc><w:p><w:r><w:t>项目A</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>负责人张三</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>""",
        )
    sid = store.register(
        "dataset",
        "doc",
        "项目.docx",
        content.getvalue(),
        [{"id": "c", "content": "项目A负责人张三"}],
    )
    e = inspect(store, sid, "c")
    assert e["native"]["tables"] == [[["项目A", "负责人张三"]]]
    pdf = PdfWriter()
    pdf.add_blank_page(width=300, height=300)
    content = io.BytesIO()
    pdf.write(content)
    sid = store.register(
        "dataset",
        "pdf",
        "扫描.pdf",
        content.getvalue(),
        [{"id": "c", "content": "OCR", "positions": [[1, 0, 0, 0, 0]]}],
    )
    e = inspect(store, sid, "c")
    assert e["native"]["page_count"] == 1
    assert any("无原生文本" in w for w in e["warnings"])


def test_scope_unbinding_hides_transitive_derived_fact(store):
    p = approve(store, setup(store))
    d = approve(store, derive(store, p))
    store.publish("管理员")
    with store.connect() as db:
        db.execute("UPDATE sources SET dataset_id='dataset'")
    scoped = Store(store.root, ["local"])
    assert scoped.verified() == []
    with pytest.raises(ValueError, match="allowlist"):
        scoped.fact(d["id"])


def test_local_checks_no_paid_call_and_repeated_run_is_incremental(store, monkeypatch):
    f = setup(store)
    store.add_candidates(
        f["source_id"], "c", [candidate(value="测试A", attribute="项目负责人")]
    )
    result = local_check(store)
    assert result["checked"] == 2 and result["flagged"] == 1
    assert local_check(store)["skipped"] == 2
    assert store.statistics()["pending"] == 2


def test_ai_check_uses_native_and_approved_context_without_approval(store, monkeypatch):
    p = approve(store, setup(store))
    store.add_candidates(p["source_id"], "c", [candidate(value="1/2")])
    f = store.fact(store.review_rows()[0]["id"])
    monkeypatch.setenv("LLM_API_KEY", "synthetic-test-key")

    def reply(request):
        payload = json.loads(request.content)
        packet = json.loads(payload["messages"][1]["content"])
        assert (
            packet["evidence"][0]["native"]["text"]
            and packet["approved_context"][0]["id"] == p["id"]
        )
        assert packet["targets"][0]["id"] == f["id"]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {
                                    "checks": [
                                        {
                                            "fact_id": f["id"],
                                            "verdict": "contradicted",
                                            "summary": "同一场景数值不同，回看原件确认前提",
                                            "premise_ids": [p["id"]],
                                            "suggestions": {"value": "2/3"},
                                        }
                                    ]
                                }
                            )
                        },
                    }
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(reply))
    config = {"llm_url": "https://api.deepseek.com", "llm_model": "deepseek-flash"}
    assert check_with_model(store, config, client)["checked"] == 1
    assert (
        store.fact(f["id"])["status"] == "pending"
        and store.fact(f["id"])["value"] == "1/2"
    )
    assert check_with_model(store, config, client)["skipped"] == 1


def test_ai_invalid_last_item_rolls_back_entire_batch(store, monkeypatch):
    f = setup(store)
    store.add_candidates(f["source_id"], "c", [candidate(value="1/2")])
    facts = store.facts_page("pending")["rows"]
    monkeypatch.setenv("LLM_API_KEY", "synthetic-test-key")
    checks = [
        {
            "fact_id": p["id"],
            "verdict": "supported",
            "summary": "支持",
            "suggestions": {},
        }
        for p in facts
    ]
    checks[-1]["suggestions"] = {"valid_from": "invalid-date"}
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": json.dumps({"checks": checks})},
                        }
                    ]
                },
            )
        )
    )
    with pytest.raises(ValueError):
        check_with_model(
            store, {"llm_url": "https://api.deepseek.com", "llm_model": "test"}, client
        )
    assert all(store.fact(p["id"])["check"] is None for p in facts)


def test_read_and_review_mcp_tokens_web_isolation_and_original_download(catalog, web):
    lid = catalog.create("测试")["id"]
    store = catalog.store(lid)
    f = setup(store)
    reader = catalog.issue_token(lid, "只读")
    reviewer = catalog.issue_token(lid, "检查", scope="review")
    assert rpc(web, reader["token"], "get_review_packet").get("isError")
    response = rpc(web, reviewer["token"], "get_review_packet")
    assert not response.get("isError"), response
    packet = json.loads(response["content"][0]["text"])
    assert packet["targets"][0]["id"] == f["id"]
    args = {
        "fact_id": f["id"],
        "expected_revision": f["revision"],
        "verdict": "supported",
        "summary": "Codex回看原件支持",
        "checker": "Codex",
    }
    assert rpc(web, reader["token"], "submit_fact_check", args).get("isError")
    assert not rpc(web, reviewer["token"], "submit_fact_check", args).get("isError")
    assert store.fact(f["id"])["status"] == "pending"
    path = "/mcp-source/" + f["source_id"] + "/original"
    assert web.get(path).status_code == 401
    assert (
        web.get(
            path, headers={"Authorization": "Bearer " + reader["token"]}
        ).status_code
        == 200
    )
    other = catalog.create("其他")["id"]
    token = catalog.issue_token(other, "其他检查", scope="review")["token"]
    assert (
        web.get(path, headers={"Authorization": "Bearer " + token}).status_code == 400
    )
    assert rpc(web, token, "get_review_packet", {"fact_ids": [f["id"]]}).get("isError")
    web.cookies.clear()
    assert (
        web.post(
            "/api/libraries/" + lid + "/publish",
            headers={"Authorization": "Bearer " + reviewer["token"]},
            json={"reviewer": "agent"},
        ).status_code
        == 401
    )
    catalog.revoke_token(lid, reviewer["id"])
    assert (
        web.post(
            "/mcp", headers={"Authorization": "Bearer " + reviewer["token"]}, json={}
        ).status_code
        == 401
    )


def test_legacy_token_scope_migration_is_read_only(tmp_path):
    db = sqlite3.connect(tmp_path / "catalog.sqlite3")
    db.executescript(
        "CREATE TABLE tokens(id TEXT PRIMARY KEY,library_id TEXT,name TEXT,token_hash TEXT,prefix TEXT,created_at TEXT,expires_at TEXT,revoked_at TEXT);"
    )
    db.execute(
        "INSERT INTO tokens VALUES (?,?,?,?,?,?,?,?)",
        (
            "id",
            "lib",
            "old",
            hashlib.sha256(b"fact_old").hexdigest(),
            "fact_old",
            "2000",
            None,
            None,
        ),
    )
    db.commit()
    db.close()
    catalog = Catalog({"state_dir": str(tmp_path)})
    assert catalog.mcp_identity("fact_old") == {"library_id": "lib", "scope": "read"}


def test_different_conditions_do_not_prove_contradiction(store):
    p = approve(store, setup(store))
    f = {**candidate(value="1/2"), "conditions": "测试B规模"}
    store.add_candidates(p["source_id"], "c", [f])
    f = store.fact(store.review_rows()[0]["id"])
    with pytest.raises(ValueError, match="exact conditions"):
        store.save_check(
            f["id"],
            "contradicted",
            "不同值",
            "agent",
            expected_revision=f["revision"],
            premise_ids=[p["id"]],
            premise_revisions={p["id"]: p["revision"]},
        )
    store.save_check(
        f["id"],
        "needs_review",
        "条件不同，不能直接推断真假",
        "agent",
        expected_revision=f["revision"],
        premise_ids=[p["id"]],
        premise_revisions={p["id"]: p["revision"]},
    )
    assert store.fact(f["id"])["status"] == "pending"


def test_mcp_derivation_scope_and_pending_only(catalog, web):
    lid = catalog.create("测试")["id"]
    store = catalog.store(lid)
    p = approve(store, setup(store))
    reader = catalog.issue_token(lid, "只读")["token"]
    reviewer = catalog.issue_token(lid, "检查", scope="review")["token"]
    args = {
        "premise_ids": [p["id"]],
        "premise_revisions": {p["id"]: p["revision"]},
        "rule": "ratio_complement",
        "reasoning": "同一对象的数学补量",
    }
    assert rpc(web, reader, "propose_derived_fact", args).get("isError")
    response = rpc(web, reviewer, "propose_derived_fact", args)
    assert not response.get("isError"), response
    derived = json.loads(response["content"][0]["text"])
    assert derived["status"] == "pending" and not derived["external_use"]
    assert store.statistics()["pending"] == 1 and store.verified() == []
    assert rpc(web, reviewer, "publish", {"reviewer": "agent"}).get("isError")
