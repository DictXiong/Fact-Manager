"""Library metadata, scoped MCP credentials, and background job records."""

import contextlib
import datetime as dt
import hashlib
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import uuid

from .core import Store, now
from .workflow import PROMPT, PROMPT_VERSION

SCHEMA = """
CREATE TABLE IF NOT EXISTS libraries (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
 prompt TEXT NOT NULL, prompt_version TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bindings (
 library_id TEXT NOT NULL REFERENCES libraries(id), dataset_id TEXT NOT NULL,
 PRIMARY KEY(library_id,dataset_id)
);
CREATE TABLE IF NOT EXISTS exclusions (
 library_id TEXT NOT NULL REFERENCES libraries(id), dataset_id TEXT NOT NULL, document_id TEXT NOT NULL,
 PRIMARY KEY(library_id,dataset_id,document_id)
);
CREATE TABLE IF NOT EXISTS tokens (
 id TEXT PRIMARY KEY, library_id TEXT NOT NULL REFERENCES libraries(id),
 name TEXT NOT NULL, token_hash TEXT UNIQUE NOT NULL, prefix TEXT NOT NULL,
 created_at TEXT NOT NULL, expires_at TEXT, revoked_at TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, library_id TEXT NOT NULL REFERENCES libraries(id), kind TEXT NOT NULL,
 status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 progress INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL DEFAULT 0,
 result TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT ''
);
"""


class Catalog:
    def __init__(self, config):
        self.config = config
        self.root = Path(config["state_dir"])
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "libraries").mkdir(exist_ok=True)
        self.db_path = self.root / "catalog.sqlite3"
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            if "scope" not in {r[1] for r in db.execute("PRAGMA table_info(tokens)")}:
                db.execute(
                    "ALTER TABLE tokens ADD COLUMN scope TEXT NOT NULL DEFAULT 'read'"
                )

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def library(self, library_id):
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", library_id):
            raise ValueError("Invalid library ID")
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM libraries WHERE id=?", (library_id,)
            ).fetchone()
            if row is None:
                raise ValueError("Unknown fact library")
            result = dict(row)
            result["dataset_ids"] = [
                r[0]
                for r in db.execute(
                    "SELECT dataset_id FROM bindings WHERE library_id=? ORDER BY dataset_id",
                    (library_id,),
                )
            ]
        return result

    def libraries(self):
        with self.connect() as db:
            ids = [
                r[0] for r in db.execute("SELECT id FROM libraries ORDER BY created_at")
            ]
        return [self.library(i) for i in ids]

    def create(
        self, name, description="", prompt=None, library_id=None, prompt_version=None
    ):
        name = str(name).strip()
        prompt = PROMPT if prompt is None else str(prompt).strip()
        if (
            not name
            or len(name) > 100
            or len(description) > 2000
            or not prompt
            or len(prompt) > 20000
        ):
            raise ValueError(
                "Provide a name (1–100 characters) and a nonempty extraction prompt"
            )
        library_id = library_id or uuid.uuid4().hex
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", library_id):
            raise ValueError("Invalid library ID")
        version = (
            prompt_version
            or PROMPT_VERSION + "-" + hashlib.sha256(prompt.encode()).hexdigest()[:12]
        )
        with self.connect() as db:
            db.execute(
                "INSERT INTO libraries VALUES (?,?,?,?,?,?,?)",
                (library_id, name, description, prompt, version, now(), now()),
            )
        return self.library(library_id)

    def update(self, library_id, name, description, prompt):
        current = self.library(library_id)
        name, prompt = str(name).strip(), str(prompt).strip()
        if (
            not name
            or len(name) > 100
            or len(description) > 2000
            or not prompt
            or len(prompt) > 20000
        ):
            raise ValueError("Invalid library name or extraction prompt")
        version = (
            current["prompt_version"]
            if prompt == current["prompt"]
            else PROMPT_VERSION + "-" + hashlib.sha256(prompt.encode()).hexdigest()[:12]
        )
        with self.connect() as db:
            db.execute(
                "UPDATE libraries SET name=?,description=?,prompt=?,prompt_version=?,updated_at=? WHERE id=?",
                (name, description, prompt, version, now(), library_id),
            )
        return self.library(library_id)

    def bind(self, library_id, dataset_id):
        self.library(library_id)
        if (
            not isinstance(dataset_id, str)
            or not dataset_id.strip()
            or len(dataset_id) > 128
            or dataset_id == "local"
        ):
            raise ValueError("Provide a RAGFlow Dataset ID")
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO bindings VALUES (?,?)",
                (library_id, dataset_id.strip()),
            )

    def unbind(self, library_id, dataset_id):
        self.library(library_id)
        with self.connect() as db:
            db.execute(
                "DELETE FROM bindings WHERE library_id=? AND dataset_id=?",
                (library_id, dataset_id),
            )
        # The current scope excludes the dataset immediately, including published facts.

    def store(self, library_id):
        library = self.library(library_id)
        return Store(
            self.root / "libraries" / library_id, library["dataset_ids"] + ["local"]
        )

    def workflow_config(self, library_id):
        library = self.library(library_id)
        return {
            **self.config,
            "prompt": library["prompt"],
            "prompt_version": library["prompt_version"],
        }

    def excluded(self, library_id):
        with self.connect() as db:
            return {
                (r[0], r[1])
                for r in db.execute(
                    "SELECT dataset_id,document_id FROM exclusions WHERE library_id=?",
                    (library_id,),
                )
            }

    def disable_source(self, library_id, source_id):
        store = self.store(library_id)
        source = store.source(source_id)
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO exclusions VALUES (?,?,?)",
                (library_id, source["dataset_id"], source["document_id"]),
            )
        with store.connect() as db:
            db.execute("UPDATE sources SET active=0 WHERE id=?", (source_id,))

    def add_text(self, library_id, name, text, document_id=None):
        if (
            not isinstance(name, str)
            or not name.strip()
            or len(name) > 255
            or not isinstance(text, str)
            or not text.strip()
            or len(text.encode()) > 4 * 1024 * 1024
        ):
            raise ValueError("Provide a source title and up to 4 MiB of UTF-8 text")
        store = self.store(library_id)
        if document_id:
            with store.connect() as db:
                if (
                    db.execute(
                        "SELECT 1 FROM sources WHERE dataset_id='local' AND document_id=?",
                        (document_id,),
                    ).fetchone()
                    is None
                ):
                    raise ValueError("Unknown local source")
        else:
            document_id = uuid.uuid4().hex
        # Overlap preserves a sentence that crosses a chunk boundary.
        chunks = [
            {"id": str(i), "content": text[start : start + 6000], "positions": []}
            for i, start in enumerate(range(0, len(text), 5500))
        ]
        sid = store.register("local", document_id, name.strip(), text.encode(), chunks)
        with self.connect() as db:
            db.execute(
                "DELETE FROM exclusions WHERE library_id=? AND dataset_id='local' AND document_id=?",
                (library_id, document_id),
            )
        return sid

    def issue_token(self, library_id, name, expires_days=None, scope="read"):
        self.library(library_id)
        name = str(name).strip()
        if not name or len(name) > 100:
            raise ValueError("Provide a token name (1–100 characters)")
        if expires_days is not None and (
            type(expires_days) is not int or not 1 <= expires_days <= 3650
        ):
            raise ValueError(
                "Token lifetime must be 1–3650 days, or empty for no expiry"
            )
        if scope not in ("read", "review"):
            raise ValueError("Token scope must be read or review")
        raw = "fact_" + secrets.token_urlsafe(32)
        token_id = uuid.uuid4().hex
        expires = (
            (
                dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=expires_days)
            ).isoformat()
            if expires_days
            else None
        )
        with self.connect() as db:
            db.execute(
                "INSERT INTO tokens(id,library_id,name,token_hash,prefix,created_at,expires_at,revoked_at,scope) VALUES (?,?,?,?,?,?,?,NULL,?)",
                (
                    token_id,
                    library_id,
                    name,
                    hashlib.sha256(raw.encode()).hexdigest(),
                    raw[:12],
                    now(),
                    expires,
                    scope,
                ),
            )
        return {
            "id": token_id,
            "name": name,
            "token": raw,
            "expires_at": expires,
            "scope": scope,
        }

    def tokens(self, library_id):
        self.library(library_id)
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT id,name,prefix,created_at,expires_at,revoked_at,scope FROM tokens WHERE library_id=? ORDER BY created_at DESC",
                    (library_id,),
                )
            ]

    def revoke_token(self, library_id, token_id):
        self.library(library_id)
        with self.connect() as db:
            changed = db.execute(
                "UPDATE tokens SET revoked_at=? WHERE library_id=? AND id=? AND revoked_at IS NULL",
                (now(), library_id, token_id),
            ).rowcount
        return bool(changed)

    def mcp_identity(self, token):
        if (
            not isinstance(token, str)
            or not token.startswith("fact_")
            or len(token) > 100
        ):
            return None
        with self.connect() as db:
            row = db.execute(
                "SELECT library_id,scope FROM tokens WHERE token_hash=? AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at>?)",
                (hashlib.sha256(token.encode()).hexdigest(), now()),
            ).fetchone()
        return dict(row) if row else None

    def authenticate_mcp(self, token):
        identity = self.mcp_identity(token)
        return identity["library_id"] if identity else None

    def create_job(self, library_id, kind):
        self.library(library_id)
        if kind not in ("sync", "extract", "check", "check-local"):
            raise ValueError("Unknown task")
        jid = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM jobs WHERE library_id=? AND status IN ('queued','running')",
                (library_id,),
            ).fetchone():
                raise ValueError("This library already has an active task")
            if (
                db.execute(
                    "SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')"
                ).fetchone()[0]
                >= 32
            ):
                raise ValueError("Task queue is full")
            db.execute(
                "INSERT INTO jobs(id,library_id,kind,status,created_at,updated_at) VALUES (?,?,?,'queued',?,?)",
                (jid, library_id, kind, now(), now()),
            )
        return jid

    def update_job(self, job_id, **fields):
        allowed = {"status", "progress", "total", "result", "error"}
        if not fields or not set(fields) <= allowed:
            raise ValueError("Invalid task update")
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET "
                + ",".join(k + "=?" for k in fields)
                + ",updated_at=? WHERE id=?",
                (*fields.values(), now(), job_id),
            )

    def jobs(self, library_id):
        self.library(library_id)
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM jobs WHERE library_id=? ORDER BY created_at DESC LIMIT 20",
                    (library_id,),
                )
            ]

    def recover_jobs(self):
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET status='failed',error='服务已重启，请重新启动任务；已完成分块会保留。',updated_at=? WHERE status IN ('queued','running')",
                (now(),),
            )

    def migrate_legacy(
        self, legacy_path, dataset_ids, name="Harnets.AI", library_id="harnets"
    ):
        legacy = Path(legacy_path)
        if not (legacy / "facts.sqlite3").is_file():
            raise ValueError("Legacy facts database does not exist")
        with self.connect() as db:
            if db.execute(
                "SELECT 1 FROM libraries WHERE id=?", (library_id,)
            ).fetchone():
                raise ValueError("Migration destination already exists")
        dest = self.root / "libraries" / library_id
        if dest.exists():
            raise ValueError("Migration directory already exists")
        dest.mkdir()
        for child in ("sources", "reviews"):
            shutil.copytree(legacy / child, dest / child)
        source = sqlite3.connect(
            "file:" + str(legacy / "facts.sqlite3") + "?mode=ro", uri=True
        )
        target = sqlite3.connect(dest / "facts.sqlite3")
        try:
            source.backup(target)
            target.execute(
                "UPDATE sources SET original_path=? || '/sources/' || id || '/original'",
                (str(dest),),
            )
            target.commit()
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Migrated database integrity check failed")
            for row in target.execute("SELECT original_path,file_hash FROM sources"):
                if hashlib.sha256(Path(row[0]).read_bytes()).hexdigest() != row[1]:
                    raise ValueError("Migrated evidence hash mismatch")
            for table in ("sources", "chunks", "facts", "events", "extractions"):
                if (
                    source.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                    != target.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                ):
                    raise ValueError("Migration count mismatch")
        finally:
            source.close()
            target.close()
        # Retain the old extraction identity to avoid repeating completed model calls.
        from .legacy_prompt import LEGACY_PROMPT

        self.create(
            name,
            "从旧版事实库迁移，保留候选事实、证据和审核记录。",
            LEGACY_PROMPT,
            library_id,
            "harnets-kb-facts-1",
        )
        for dataset in dataset_ids:
            self.bind(library_id, dataset)
        return self.library(library_id)
