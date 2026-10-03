import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import hashlib
import hmac
from http.cookies import CookieError, SimpleCookie
import io
import json
import os
from pathlib import Path
import secrets
import time
import zipfile

import httpx
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
import uvicorn

from .checking import check_with_model, local_check, review_packet
from .evidence import inspect
from .workflow import PROMPT, Ragflow, export_review, extract, import_review

ASSETS = Path(__file__).parent / "static"
COOKIE = "fact_session"
COOKIE_MAX_AGE = 400 * 24 * 3600


def set_session_cookie(response, session):
    response.set_cookie(
        COOKIE,
        session,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
    )


def admin_token(config):
    path = config.get("admin_token_file")
    value = (
        Path(path).read_text().strip()
        if path
        else os.environ.get("FACT_ADMIN_TOKEN", "")
    )
    if len(value) < 16:
        raise ValueError("An administrator token of at least 16 characters is required")
    return value


class Authentication:
    """Admin web sessions and per-library MCP credentials never grant each other's access."""

    def __init__(self, app, catalog, token, public_host):
        self.app, self.catalog, self.token = app, catalog, token
        self.public_host = public_host

    def session(self, cookie):
        try:
            jar = SimpleCookie()
            jar.load(cookie)
            value = jar[COOKIE].value
            payload, signature = value.rsplit(".", 1)
            expected = hmac.new(
                self.token.encode(), payload.encode(), hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(signature, expected):
                return None
            data = json.loads(base64.urlsafe_b64decode(payload))
            if (
                not isinstance(data, dict)
                or not isinstance(data.get("nonce"), str)
                or not data["nonce"]
            ):
                return None
            # Older signed sessions retain their original expiry until a valid
            # authenticated request upgrades them to the persistent format.
            if data["expires"] is not None and data["expires"] < time.time():
                return None
            return data
        except (ValueError, KeyError, TypeError, CookieError):
            return None

    def csrf(self, session):
        return hmac.new(
            self.token.encode(), ("csrf:" + session["nonce"]).encode(), hashlib.sha256
        ).hexdigest()

    def _sign_session(self, data):
        payload = base64.urlsafe_b64encode(json.dumps(data).encode()).decode()
        signature = hmac.new(
            self.token.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        return payload + "." + signature

    def issue_session(self):
        data = {"expires": None, "nonce": secrets.token_urlsafe(24)}
        return self._sign_session(data), self.csrf(data)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        path = scope["path"]
        scope.setdefault("state", {})
        renew_cookie = None
        supplied = headers.get(b"authorization", b"").decode(errors="replace")
        bearer = supplied[7:] if supplied.startswith("Bearer ") else ""
        if (
            path == "/mcp"
            or path.startswith("/mcp/")
            or path.startswith("/mcp-source/")
        ):
            identity = self.catalog.mcp_identity(bearer)
            if not identity:
                return await JSONResponse(
                    {"error": "unauthorized"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )(scope, receive, send)
            scope["state"]["library_id"] = identity["library_id"]
            scope["state"]["mcp_scope"] = identity["scope"]
        elif path.startswith("/api/") and path != "/api/login":
            session = self.session(headers.get(b"cookie", b"").decode(errors="replace"))
            if not session:
                return await JSONResponse(
                    {"error": "请先使用管理员 Token 登录。"}, status_code=401
                )(scope, receive, send)
            if scope["method"] not in ("GET", "HEAD", "OPTIONS"):
                csrf = headers.get(b"x-fact-csrf", b"").decode(errors="replace")
                if not hmac.compare_digest(csrf, self.csrf(session)):
                    return await JSONResponse(
                        {"error": "Invalid CSRF token"}, status_code=403
                    )(scope, receive, send)
            scope["state"]["csrf"] = self.csrf(session)
            if path != "/api/logout":
                # Keep the nonce so existing tabs retain their CSRF token.
                renew_cookie = self._sign_session(
                    {"expires": None, "nonce": session["nonce"]}
                )
        if path.startswith("/api/") and scope["method"] not in ("GET", "HEAD"):
            origin = headers.get(b"origin", b"").decode(errors="replace")
            allowed = {
                "https://" + self.public_host,
                "https://127.0.0.1",
                "http://127.0.0.1",
                "http://localhost",
                "https://localhost",
            }
            if origin and origin not in allowed:
                return await JSONResponse(
                    {"error": "Invalid request origin"}, status_code=403
                )(scope, receive, send)
        # Cap request bodies before parsers allocate memory. MCP requests remain streaming.
        if path.startswith("/api/") and scope["method"] not in ("GET", "HEAD"):
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > 8 * 1024 * 1024:
                    return await JSONResponse(
                        {"error": "Upload exceeds 8 MiB"}, status_code=413
                    )(scope, receive, send)
                if not message.get("more_body"):
                    break
            delivered = False
            original_receive = receive

            async def replay():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {
                        "type": "http.request",
                        "body": bytes(body),
                        "more_body": False,
                    }
                return await original_receive()

            receive = replay

        async def secure_send(message):
            if message["type"] == "http.response.start":
                if renew_cookie is not None and 200 <= message["status"] < 400:
                    refreshed = Response()
                    set_session_cookie(refreshed, renew_cookie)
                    message.setdefault("headers", []).extend(
                        (name, value)
                        for name, value in refreshed.raw_headers
                        if name == b"set-cookie"
                    )
                message.setdefault("headers", []).extend(
                    [
                        (b"cache-control", b"no-store"),
                        (b"x-content-type-options", b"nosniff"),
                        (
                            b"content-security-policy",
                            b"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
                        ),
                    ]
                )
            await send(message)

        await self.app(scope, receive, secure_send)


class Jobs:
    def __init__(self, catalog):
        self.catalog = catalog
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fact-job")

    def submit(self, library_id, kind):
        jid = self.catalog.create_job(library_id, kind)
        self.executor.submit(self.run, jid, library_id, kind)
        return jid

    def run(self, jid, library_id, kind):
        try:
            self.catalog.update_job(jid, status="running")
            store = self.catalog.store(library_id)
            config = self.catalog.workflow_config(library_id)
            with store.workflow_lock():
                if kind == "sync":
                    with Ragflow(config) as ragflow:
                        result = {
                            "snapshotted_documents": ragflow.sync(
                                store, self.catalog.excluded(library_id)
                            )
                        }
                elif kind == "extract":
                    result = {
                        "new_candidates": extract(
                            store,
                            config,
                            progress=lambda done, total: self.catalog.update_job(
                                jid, progress=done, total=total
                            ),
                        )
                    }
                elif kind == "check":
                    result = check_with_model(
                        store,
                        config,
                        progress=lambda done, total: self.catalog.update_job(
                            jid, progress=done, total=total
                        ),
                    )
                else:
                    result = local_check(
                        store,
                        progress=lambda done, total: self.catalog.update_job(
                            jid, progress=done, total=total
                        ),
                    )
                if kind in ("sync", "extract"):
                    result["local_check"] = local_check(store)
            self.catalog.update_job(jid, status="succeeded", result=json.dumps(result))
        except Exception as error:
            # Keys and authorization headers are never part of the task error response.
            self.catalog.update_job(jid, status="failed", error=str(error)[:1000])


def make_app(catalog, config):
    token = admin_token(config)
    public_host = config.get("public_host", "localhost")
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            "127.0.0.1",
            "127.0.0.1:*",
            "localhost",
            "localhost:*",
            public_host,
            public_host + ":443",
        ],
        allowed_origins=[
            "http://127.0.0.1",
            "http://127.0.0.1:*",
            "https://127.0.0.1",
            "http://localhost",
            "http://localhost:*",
            "https://" + public_host,
        ],
    )
    server = FastMCP(
        "fact-manager",
        stateless_http=True,
        json_response=True,
        transport_security=security,
    )

    def scoped(ctx, review=False):
        request = ctx.request_context.request
        if request is None or not getattr(request.state, "library_id", None):
            raise ValueError("A library-scoped MCP credential is required")
        if review and getattr(request.state, "mcp_scope", "read") != "review":
            raise ValueError(
                "This tool requires a review Token; approval and publication remain administrator-only"
            )
        library_id = request.state.library_id
        return library_id, catalog.store(library_id)

    @server.tool()
    def get_library(ctx: Context) -> dict:
        """Describe the fact library authorized by this token, including counts and available sources."""
        library_id, store = scoped(ctx)
        library = catalog.library(library_id)
        return {
            "id": library_id,
            "name": library["name"],
            "description": library["description"],
            "statistics": store.statistics(),
        }

    @server.tool()
    def get_verified_facts(
        ctx: Context, entity: str = "", attribute: str = "", for_external: bool = True
    ) -> list[dict]:
        """Get published, current, in-date facts from this token's library. External use requires explicit approval."""
        _, store = scoped(ctx)
        # Original filesystem paths and candidate history are not part of the MCP result.
        return store.verified(entity, attribute, for_external)

    @server.tool()
    def search_evidence(ctx: Context, query: str, limit: int = 8) -> list[dict]:
        """Search this library's active evidence. Evidence is not a human-reviewed fact."""
        library_id, store = scoped(ctx)
        if not query.strip() or not 1 <= limit <= 20:
            raise ValueError("Provide a query and limit between 1 and 20")
        local = store.search_local(query, limit)
        with Ragflow(catalog.workflow_config(library_id)) as ragflow:
            remote = ragflow.search(store, query, limit)
        return (local + remote)[:limit]

    @server.tool()
    def read_source(ctx: Context, source_id: str, chunk_id: str) -> dict:
        """Read an immutable active evidence chunk from the token's library, with its source hash."""
        _, store = scoped(ctx)
        source = store.source(source_id)
        for chunk in store.chunks(source_id):
            if chunk["id"] == chunk_id:
                return {
                    "source_id": source_id,
                    "source_name": source["name"],
                    "file_sha256": source["file_hash"],
                    "chunk_id": chunk_id,
                    "content": chunk["content"],
                    "positions": json.loads(chunk["positions"]),
                    "review_status": "evidence_only",
                }
        raise ValueError("Unknown evidence chunk")

    @server.tool()
    def inspect_source(
        ctx: Context, source_id: str, chunk_id: str | None = None
    ) -> dict:
        """Compare original native structure with RAGFlow evidence; images remain unverified. Scoped Bearer download is provided."""
        _, store = scoped(ctx)
        result = inspect(store, source_id, chunk_id)
        result["original_download_url"] = (
            "https://" + public_host + "/mcp-source/" + source_id + "/original"
        )
        return result

    @server.tool()
    def get_review_packet(
        ctx: Context,
        fact_ids: list[str] | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> dict:
        """REVIEW Token: obtain pending targets, native originals and bounded approved premises with revision guards. Not approval."""
        _, store = scoped(ctx, review=True)
        return review_packet(store, fact_ids, offset, limit)

    @server.tool()
    def submit_fact_check(
        ctx: Context,
        fact_id: str,
        expected_revision: str,
        verdict: str,
        summary: str,
        checker: str,
        suggestions: dict | None = None,
        premise_ids: list[str] | None = None,
        premise_revisions: dict | None = None,
        context_revision: str = "",
    ) -> dict:
        """REVIEW Token: save advisory check and suggested fields; cannot approve, modify evidence or publish. Cite current approved premises."""
        _, store = scoped(ctx, review=True)
        return store.save_check(
            fact_id,
            verdict,
            summary,
            checker,
            expected_revision=expected_revision,
            suggestions=suggestions,
            premise_ids=premise_ids,
            premise_revisions=premise_revisions,
            context_revision=context_revision,
        )

    @server.tool()
    def propose_derived_fact(
        ctx: Context,
        premise_ids: list[str],
        premise_revisions: dict,
        reasoning: str,
        rule: str = "reasoned",
        candidate: dict | None = None,
        proposer: str = "MCP agent",
    ) -> dict:
        """REVIEW Token: create a PENDING derivation from eligible approved premises. ratio_complement computes the mathematical complement 1-ratio, not an automatic reduction rate; general reasoning needs human verification."""
        _, store = scoped(ctx, review=True)
        return store.propose_derived(
            premise_ids,
            reasoning,
            rule=rule,
            candidate=candidate,
            actor=proposer,
            premise_revisions=premise_revisions,
        )

    app = server.streamable_http_app()
    jobs = Jobs(catalog)
    auth = Authentication(app, catalog, token, public_host)
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        catalog.recover_jobs()
        async with original_lifespan(application):
            yield
        jobs.executor.shutdown(wait=False, cancel_futures=True)

    app.router.lifespan_context = lifespan

    async def login(request):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("Login JSON must be an object")
            if not hmac.compare_digest(
                str(body.get("token", "")).encode(), token.encode()
            ):
                return JSONResponse({"error": "管理员 Token 不正确。"}, status_code=401)
            session, csrf = auth.issue_session()
            response = JSONResponse({"csrf": csrf})
            set_session_cookie(response, session)
            return response
        except (ValueError, TypeError):
            return JSONResponse({"error": "Invalid login request"}, status_code=400)

    async def logout(request):
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/")
        return response

    def dispatch(request, data, form):
        try:
            path = request.path_params.get("path", "").strip("/").split("/")
            method = request.method
            if path == ["session"] and method == "GET":
                return JSONResponse({"csrf": request.state.csrf})
            if path == ["settings"] and method == "GET":
                return JSONResponse(
                    {
                        "ragflow_url": config.get("ragflow_public_url", ""),
                        "llm_model": config["llm_model"],
                        "default_prompt": PROMPT,
                        "mcp_url": "https://" + public_host + "/mcp",
                    }
                )
            if path == ["ragflow", "datasets"] and method == "GET":
                result = []
                page = 1
                with Ragflow(config) as ragflow:
                    while True:
                        rows = ragflow.data(
                            "GET", "/datasets", params={"page": page, "page_size": 100}
                        )
                        result.extend({"id": r["id"], "name": r["name"]} for r in rows)
                        if len(rows) < 100:
                            break
                        page += 1
                return JSONResponse(result)
            if path == ["libraries"]:
                if method == "GET":
                    return JSONResponse(
                        [
                            {
                                **library,
                                "statistics": catalog.store(library["id"]).statistics(),
                            }
                            for library in catalog.libraries()
                        ]
                    )
                if method == "POST":
                    return JSONResponse(
                        catalog.create(
                            data["name"],
                            data.get("description", ""),
                            data.get("prompt"),
                        ),
                        status_code=201,
                    )
            if len(path) < 2 or path[0] != "libraries":
                return JSONResponse({"error": "Not found"}, status_code=404)
            lid = path[1]
            library = catalog.library(lid)
            store = catalog.store(lid)
            tail = path[2:]
            if not tail:
                if method == "GET":
                    return JSONResponse({**library, "statistics": store.statistics()})
                if method == "PATCH":
                    return JSONResponse(
                        catalog.update(
                            lid,
                            data["name"],
                            data.get("description", ""),
                            data["prompt"],
                        )
                    )
            if tail == ["bindings"] and method == "POST":
                dataset = data["dataset_id"]
                with Ragflow(config) as ragflow:
                    rows = ragflow.data(
                        "GET", "/datasets", params={"id": dataset, "page_size": 1}
                    )
                if not any(r["id"] == dataset for r in rows):
                    raise ValueError(
                        "Dataset is not accessible with the configured RAGFlow API key"
                    )
                catalog.bind(lid, dataset)
                return JSONResponse({"ok": True})
            if len(tail) == 2 and tail[0] == "bindings" and method == "DELETE":
                catalog.unbind(lid, tail[1])
                return JSONResponse({"ok": True})
            if tail == ["sources"] and method == "GET":
                return JSONResponse(
                    [
                        {k: v for k, v in s.items() if k != "original_path"}
                        for s in store.sources()
                    ]
                )
            if tail == ["sources", "text"] and method == "POST":
                sid = catalog.add_text(
                    lid, data["name"], data["text"], data.get("document_id")
                )
                return JSONResponse({"source_id": sid}, status_code=201)
            if tail == ["sources", "upload"] and method == "POST":
                file = form["file"]
                name = file["name"]
                if Path(name).suffix.lower() not in (".txt", ".md", ".markdown"):
                    raise ValueError(
                        "Direct uploads support UTF-8 TXT and Markdown; use RAGFlow for PDF/Word"
                    )
                sid = catalog.add_text(lid, name, file["content"].decode("utf-8-sig"))
                return JSONResponse({"source_id": sid}, status_code=201)
            if len(tail) >= 2 and tail[0] == "sources":
                sid = tail[1]
                if len(tail) == 2 and method == "DELETE":
                    catalog.disable_source(lid, sid)
                    return JSONResponse({"ok": True})
                if tail[2:] == ["chunks"] and method == "GET":
                    return JSONResponse(store.chunks(sid))
                if tail[2:] == ["inspect"] and method == "GET":
                    return JSONResponse(
                        inspect(store, sid, request.query_params.get("chunk_id"))
                    )
                if tail[2:] == ["original"] and method == "GET":
                    source = store.source(sid)
                    inspect(store, sid)
                    return FileResponse(
                        source["original_path"],
                        filename=source["name"],
                        media_type="application/octet-stream",
                    )
            if tail == ["facts"]:
                if method == "GET":
                    q = request.query_params
                    return JSONResponse(
                        store.facts_page(
                            q.get("status", ""),
                            q.get("query", ""),
                            int(q.get("offset", 0)),
                            int(q.get("limit", 100)),
                            q.get("history") == "1",
                        )
                    )
                if method == "POST":
                    added = store.add_candidates(
                        data["source_id"], data["chunk_id"], [data["fact"]]
                    )
                    return JSONResponse(
                        {"new_candidates": added, "local_check": local_check(store)},
                        status_code=201,
                    )
            if tail == ["review-packet"] and method == "GET":
                q = request.query_params
                return JSONResponse(
                    review_packet(
                        store,
                        offset=int(q.get("offset", 0)),
                        limit=int(q.get("limit", 20)),
                    )
                )
            if tail == ["approved-premises"] and method == "GET":
                return JSONResponse(store.approved_facts())
            if tail == ["derivations"] and method == "POST":
                return JSONResponse(
                    store.propose_derived(
                        data["premise_ids"],
                        data["reasoning"],
                        rule=data.get("rule", "reasoned"),
                        candidate=data.get("candidate"),
                        actor=data.get("proposer", "管理员"),
                        premise_revisions=data.get("premise_revisions"),
                    ),
                    status_code=201,
                )
            if (
                len(tail) == 3
                and tail[0] == "facts"
                and tail[2] == "check"
                and method == "POST"
            ):
                return JSONResponse(
                    store.save_check(
                        tail[1],
                        data["verdict"],
                        data["summary"],
                        data["checker"],
                        suggestions=data.get("suggestions"),
                        premise_ids=data.get("premise_ids"),
                        premise_revisions=data.get("premise_revisions"),
                        expected_revision=data["expected_revision"],
                        context_revision=data.get("context_revision", ""),
                    )
                )
            if tail == ["review"] and method == "POST":
                store.review(data["changes"], data["reviewer"])
                return JSONResponse({"reviewed": len(data["changes"])})
            if tail == ["publish"] and method == "POST":
                return JSONResponse({"published": store.publish(data["reviewer"])})
            if (
                len(tail) == 3
                and tail[0] == "facts"
                and tail[2] == "retire"
                and method == "POST"
            ):
                store.retire(tail[1], data["reviewer"], data["reason"])
                return JSONResponse({"ok": True})
            if (
                len(tail) == 3
                and tail[0] == "facts"
                and tail[2] == "audit"
                and method == "GET"
            ):
                return JSONResponse(store.audit(tail[1]))
            if tail == ["review.xlsx"] and method == "GET":
                output = (
                    store.root
                    / "reviews"
                    / ("export-" + secrets.token_hex(12) + ".xlsx")
                )
                count = export_review(store, output)
                return FileResponse(
                    output,
                    filename="fact-review-" + lid + ".xlsx",
                    headers={"X-Fact-Count": str(count)},
                )
            if tail == ["review-import"] and method == "POST":
                content = form["file"]["content"]
                with zipfile.ZipFile(io.BytesIO(content)) as archive:
                    if sum(i.file_size for i in archive.infolist()) > 64 * 1024 * 1024:
                        raise ValueError("Workbook expands to more than 64 MiB")
                temp = (
                    store.root
                    / "reviews"
                    / ("upload-" + secrets.token_hex(12) + ".xlsx")
                )
                temp.write_bytes(content)
                try:
                    return JSONResponse(
                        {"reviewed": import_review(store, temp, str(form["reviewer"]))}
                    )
                finally:
                    temp.unlink(missing_ok=True)
            if tail == ["jobs"]:
                if method == "GET":
                    return JSONResponse(catalog.jobs(lid))
                if method == "POST":
                    return JSONResponse(
                        {"id": jobs.submit(lid, data["kind"])}, status_code=202
                    )
            if tail == ["tokens"]:
                if method == "GET":
                    return JSONResponse(catalog.tokens(lid))
                if method == "POST":
                    return JSONResponse(
                        catalog.issue_token(
                            lid,
                            data["name"],
                            data.get("expires_days"),
                            data.get("scope", "read"),
                        ),
                        status_code=201,
                    )
            if len(tail) == 2 and tail[0] == "tokens" and method == "DELETE":
                return JSONResponse({"revoked": catalog.revoke_token(lid, tail[1])})
            return JSONResponse({"error": "Not found"}, status_code=404)
        except (ValueError, KeyError, TypeError, zipfile.BadZipFile) as error:
            return JSONResponse({"error": str(error)}, status_code=400)
        except httpx.HTTPError as error:
            return JSONResponse({"error": str(error)}, status_code=502)

    async def api(request):
        try:
            data, form = {}, {}
            if request.method in ("POST", "PATCH"):
                path = request.path_params.get("path", "").strip("/").split("/")
                upload = (
                    request.method == "POST"
                    and len(path) == 4
                    and path[:1] == ["libraries"]
                    and path[2:] == ["sources", "upload"]
                )
                review_upload = (
                    request.method == "POST"
                    and len(path) == 3
                    and path[:1] == ["libraries"]
                    and path[2:] == ["review-import"]
                )
                if upload or review_upload:
                    async with request.form(max_files=1, max_fields=1) as parsed:
                        file = parsed["file"]
                        form = {
                            "file": {
                                "name": file.filename or "",
                                "content": await file.read(),
                            }
                        }
                        if review_upload:
                            form["reviewer"] = parsed["reviewer"]
                else:
                    data = await request.json()
                    if not isinstance(data, dict):
                        raise ValueError("Request JSON must be an object")
            return await run_in_threadpool(dispatch, request, data, form)
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            return JSONResponse({"error": str(error)}, status_code=400)

    def mcp_original(request):
        try:
            store = catalog.store(request.state.library_id)
            source = store.source(request.path_params["source_id"])
            inspect(store, source["id"])
            return FileResponse(
                source["original_path"],
                filename=source["name"],
                media_type="application/octet-stream",
            )
        except (ValueError, KeyError) as error:
            return JSONResponse({"error": str(error)}, status_code=400)

    app.routes.extend(
        [
            Route(
                "/",
                lambda request: FileResponse(ASSETS / "index.html"),
                methods=["GET"],
            ),
            Route(
                "/health", lambda request: JSONResponse({"ok": True}), methods=["GET"]
            ),
            Route("/mcp-source/{source_id}/original", mcp_original, methods=["GET"]),
            Route("/api/login", login, methods=["POST"]),
            Route("/api/logout", logout, methods=["POST"]),
            Route("/api/{path:path}", api, methods=["GET", "POST", "PATCH", "DELETE"]),
            Mount("/static", StaticFiles(directory=ASSETS), name="static"),
        ]
    )
    return auth


def serve(catalog, config):
    uvicorn.run(
        make_app(catalog, config),
        host=config.get("host", "127.0.0.1"),
        port=config.get("port", 9382),
    )
