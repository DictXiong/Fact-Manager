import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid

FIELDS = (
    "entity",
    "attribute",
    "value",
    "unit",
    "conditions",
    "valid_from",
    "valid_until",
)
SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
 id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL, document_id TEXT NOT NULL,
 name TEXT NOT NULL, file_hash TEXT NOT NULL, chunks_hash TEXT NOT NULL,
 original_path TEXT NOT NULL, created_at TEXT NOT NULL, active INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS current_document ON sources(dataset_id,document_id) WHERE active=1;
CREATE TABLE IF NOT EXISTS chunks (
 source_id TEXT NOT NULL REFERENCES sources(id), id TEXT NOT NULL,
 content TEXT NOT NULL, positions TEXT NOT NULL, PRIMARY KEY(source_id,id)
);
CREATE TABLE IF NOT EXISTS facts (
 id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id), chunk_id TEXT NOT NULL,
 entity TEXT NOT NULL, attribute TEXT NOT NULL, value TEXT NOT NULL,
 unit TEXT NOT NULL, conditions TEXT NOT NULL, valid_from TEXT NOT NULL, valid_until TEXT NOT NULL,
 quote TEXT NOT NULL, external_use INTEGER NOT NULL DEFAULT 0,
 status TEXT NOT NULL CHECK(status IN ('pending','approved','published','rejected','retired')),
 fingerprint TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
 reviewer TEXT, reviewed_at TEXT, published_at TEXT,
 FOREIGN KEY(source_id,chunk_id) REFERENCES chunks(source_id,id)
);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY, fact_id TEXT NOT NULL REFERENCES facts(id),
 action TEXT NOT NULL, actor TEXT NOT NULL, at TEXT NOT NULL, detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS extraction_facts (
 source_id TEXT NOT NULL, chunk_id TEXT NOT NULL, part INTEGER NOT NULL,
 model TEXT NOT NULL, prompt_version TEXT NOT NULL,
 fingerprint TEXT NOT NULL REFERENCES facts(fingerprint),
 PRIMARY KEY(source_id,chunk_id,part,model,prompt_version,fingerprint)
);
CREATE TABLE IF NOT EXISTS extractions (
 source_id TEXT NOT NULL, chunk_id TEXT NOT NULL, part INTEGER NOT NULL,
 model TEXT NOT NULL, prompt_version TEXT NOT NULL,
 PRIMARY KEY(source_id,chunk_id,part,model,prompt_version)
);
"""


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def load_config(path):
    config = json.loads(Path(path).read_text())
    env_path = config.get("env_file")
    if env_path and Path(env_path).exists():
        for line in Path(env_path).read_text().splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                key, sep, value = line.partition("=")
                if not sep or not key.isidentifier():
                    raise ValueError("Invalid runtime environment file")
                # This deliberately does not evaluate shell expansions.
                os.environ.setdefault(key, value)
    return config


def validate_fields(values):
    for field in FIELDS:
        if not isinstance(values.get(field, ""), str):
            raise ValueError(f"{field} must be text")
    for field in ("entity", "attribute", "value"):
        if not values.get(field, "").strip():
            raise ValueError(f"{field} is required")
    for field in ("valid_from", "valid_until"):
        if values.get(field):
            if dt.date.fromisoformat(values[field]).isoformat() != values[field]:
                raise ValueError(f"{field} must use YYYY-MM-DD")
    if (
        values.get("valid_from")
        and values.get("valid_until")
        and values["valid_from"] > values["valid_until"]
    ):
        raise ValueError("valid_until precedes valid_from")


def overlaps(left, right):
    return max(
        left["valid_from"] or "0001-01-01", right["valid_from"] or "0001-01-01"
    ) <= min(left["valid_until"] or "9999-12-31", right["valid_until"] or "9999-12-31")


class WorkflowBusy(ValueError):
    pass


class Store:
    def __init__(self, state_dir, dataset_ids):
        self.root = Path(state_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        for child in ("sources", "reviews"):
            (self.root / child).mkdir(exist_ok=True)
        self.dataset_ids = set(dataset_ids)
        self.db_path = self.root / "facts.sqlite3"
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            from .reasoning import SCHEMA as REASONING_SCHEMA

            db.executescript(REASONING_SCHEMA)

    @contextlib.contextmanager
    def workflow_lock(self):
        """Serialize long workflows across web jobs and CLI invocations."""
        with (self.root / "workflow.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise WorkflowBusy(
                    "Another sync, extraction or check is running in this library; retry later"
                ) from None
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

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

    def require_dataset(self, dataset_id):
        if dataset_id not in self.dataset_ids:
            raise ValueError(
                "Dataset is not in the configured allowlist; an empty allowlist permits no datasets"
            )

    def sources(self):
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM sources WHERE active=1 ORDER BY created_at"
                )
                if row["dataset_id"] in self.dataset_ids
            ]

    def register(self, dataset_id, document_id, name, file_bytes, chunks):
        self.require_dataset(dataset_id)
        if not chunks or len({c["id"] for c in chunks}) != len(chunks):
            raise ValueError(
                "Source must have a complete, nonempty set of unique chunks"
            )
        normalized = [
            {
                "id": c["id"],
                "content": c["content"],
                "positions": c.get("positions", []),
            }
            for c in chunks
        ]
        file_hash = hashlib.sha256(file_bytes).hexdigest()
        chunks_hash = digest(sorted(normalized, key=lambda c: c["id"]))
        source_id = digest([dataset_id, document_id, file_hash, chunks_hash])
        folder = self.root / "sources" / source_id
        folder.mkdir(exist_ok=True)
        original = folder / "original"
        if not original.exists():
            original.write_bytes(file_bytes)
        (folder / "chunks.json").write_text(
            json.dumps(normalized, ensure_ascii=False, indent=2)
        )
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE sources SET active=0 WHERE dataset_id=? AND document_id=? AND id!=?",
                (dataset_id, document_id, source_id),
            )
            db.execute(
                "INSERT OR IGNORE INTO sources VALUES (?,?,?,?,?,?,?,?,1)",
                (
                    source_id,
                    dataset_id,
                    document_id,
                    name,
                    file_hash,
                    chunks_hash,
                    str(original),
                    now(),
                ),
            )
            db.execute("UPDATE sources SET active=1 WHERE id=?", (source_id,))
            db.executemany(
                "INSERT OR IGNORE INTO chunks VALUES (?,?,?,?)",
                [
                    (source_id, c["id"], c["content"], json.dumps(c["positions"]))
                    for c in normalized
                ],
            )
        return source_id

    def source(self, source_id):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM sources WHERE id=? AND active=1", (source_id,)
            ).fetchone()
            if row is None:
                raise ValueError("Source is missing or superseded")
            self.require_dataset(row["dataset_id"])
            return dict(row)

    def chunks(self, source_id):
        self.source(source_id)
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM chunks WHERE source_id=? ORDER BY rowid",
                    (source_id,),
                )
            ]

    def add_candidates(self, source_id, chunk_id, candidates, extraction=None):
        self.source(source_id)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            chunk = db.execute(
                "SELECT content FROM chunks WHERE source_id=? AND id=?",
                (source_id, chunk_id),
            ).fetchone()
            if chunk is None:
                raise ValueError("Unknown evidence chunk")
            added = 0
            for candidate in candidates:
                validate_fields(candidate)
                quote = candidate.get("quote", "")
                if (
                    not isinstance(quote, str)
                    or not quote.strip()
                    or quote not in chunk["content"]
                ):
                    raise ValueError(
                        "Evidence quote must be a literal substring of its archived chunk"
                    )
                values = {field: candidate.get(field, "").strip() for field in FIELDS}
                fingerprint = digest([source_id, chunk_id, values, quote])
                existing = None
                if extraction is not None:
                    # Same source/version and identical semantic fields: keep the original evidence.
                    existing = db.execute(
                        """SELECT f.fingerprint FROM facts f
                        JOIN extraction_facts x ON x.fingerprint=f.fingerprint
                        WHERE x.source_id=? AND x.model=? AND x.prompt_version=?
                        AND f.entity=? AND f.attribute=? AND f.value=? AND f.unit=?
                        AND f.conditions=? AND f.valid_from=? AND f.valid_until=? LIMIT 1""",
                        (
                            source_id,
                            extraction[1],
                            extraction[2],
                            *(values[f] for f in FIELDS),
                        ),
                    ).fetchone()
                if existing:
                    fingerprint = existing["fingerprint"]
                else:
                    result = db.execute(
                        """INSERT OR IGNORE INTO facts
                        (id,source_id,chunk_id,entity,attribute,value,unit,conditions,valid_from,valid_until,
                         quote,status,fingerprint,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,'pending',?,?)""",
                        (
                            uuid.uuid4().hex,
                            source_id,
                            chunk_id,
                            *(values[f] for f in FIELDS),
                            quote,
                            fingerprint,
                            now(),
                        ),
                    )
                    added += result.rowcount
                if extraction is not None:
                    db.execute(
                        "INSERT OR IGNORE INTO extraction_facts VALUES (?,?,?,?,?,?)",
                        (source_id, chunk_id, *extraction, fingerprint),
                    )
            if extraction is not None:
                db.execute(
                    "INSERT OR IGNORE INTO extractions VALUES (?,?,?,?,?)",
                    (source_id, chunk_id, *extraction),
                )
            return added

    def extracted(self, source_id, chunk_id, part, model, prompt_version):
        with self.connect() as db:
            return (
                db.execute(
                    "SELECT 1 FROM extractions WHERE source_id=? AND chunk_id=? AND part=? AND model=? AND prompt_version=?",
                    (source_id, chunk_id, part, model, prompt_version),
                ).fetchone()
                is not None
            )

    def extraction_candidates(self, source_id, model, prompt_version):
        self.source(source_id)
        with self.connect() as db:
            rows = db.execute(
                """SELECT DISTINCT f.entity,f.attribute,f.value,f.unit,f.conditions,f.valid_from,f.valid_until
                FROM facts f JOIN extraction_facts x ON x.fingerprint=f.fingerprint
                WHERE x.source_id=? AND x.model=? AND x.prompt_version=?
                ORDER BY f.created_at DESC,f.id LIMIT 400""",
                (source_id, model, prompt_version),
            )
            return [dict(r) for r in rows]

    def review_rows(self):
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    """SELECT f.*,s.name,s.dataset_id,s.file_hash,s.active AS source_active,c.positions
                FROM facts f JOIN sources s ON s.id=f.source_id
                JOIN chunks c ON c.source_id=f.source_id AND c.id=f.chunk_id
                WHERE f.status='pending' AND s.active=1 ORDER BY f.created_at,f.id"""
                )
                if row["dataset_id"] in self.dataset_ids
            ]

    def review(self, changes, reviewer):
        if not reviewer.strip():
            raise ValueError("A reviewer is required")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            seen = set()
            for change in changes:
                if change["id"] in seen:
                    raise ValueError("Duplicate fact in review workbook")
                seen.add(change["id"])
                row = db.execute(
                    "SELECT f.*,s.active,s.dataset_id,s.file_hash FROM facts f JOIN sources s ON s.id=f.source_id WHERE f.id=?",
                    (change["id"],),
                ).fetchone()
                if row is None or row["status"] != "pending" or not row["active"]:
                    raise ValueError(
                        "Review is stale, already imported, or source is superseded"
                    )
                self.require_dataset(row["dataset_id"])
                if change["fingerprint"] != row["fingerprint"]:
                    raise ValueError(
                        "Review fingerprint does not match archived candidate"
                    )
                for field in ("source_id", "chunk_id", "quote"):
                    if change.get(field) != row[field]:
                        raise ValueError(
                            "Evidence fields in the review workbook were modified"
                        )
                if change.get("file_sha256") != row["file_hash"]:
                    raise ValueError("Source hash in the review workbook was modified")
                if change["decision"] not in ("approve", "reject"):
                    raise ValueError("Decision must be approve or reject")
                values = {field: change.get(field, "") for field in FIELDS}
                validate_fields(values)
                if change["external_use"] not in ("yes", "no"):
                    raise ValueError("external_use must be yes or no")
                if change["decision"] == "approve":
                    from .evidence import inspect

                    inspect(self, row["source_id"], row["chunk_id"])
                    from .reasoning import derivation_info, usable

                    detailed = {**dict(row), "source_active": row["active"]}
                    origin = derivation_info(self, db, detailed)
                    if origin["kind"] == "derived":
                        if not origin["current"]:
                            raise ValueError(
                                "Derived premise changed; create and check a new derivation before approval"
                            )
                        if any(values[f] != row[f] for f in FIELDS):
                            raise ValueError(
                                "Do not edit a derived conclusion independently of its reasoning; create a new proposal"
                            )
                        if change["external_use"] == "yes":
                            for premise in origin["premises"]:
                                from .reasoning import fact_row

                                if not usable(
                                    self,
                                    db,
                                    fact_row(self, db, premise["id"]),
                                    external=True,
                                )[0]:
                                    raise ValueError(
                                        "A derivation premise does not permit external use"
                                    )
                status = "approved" if change["decision"] == "approve" else "rejected"
                db.execute(
                    """UPDATE facts SET entity=?,attribute=?,value=?,unit=?,conditions=?,valid_from=?,valid_until=?,
                    external_use=?,status=?,reviewer=?,reviewed_at=? WHERE id=?""",
                    (
                        *(values[f] for f in FIELDS),
                        change["external_use"] == "yes",
                        status,
                        reviewer,
                        now(),
                        change["id"],
                    ),
                )
                self.event(
                    db,
                    change["id"],
                    "review",
                    reviewer,
                    {"before": {f: row[f] for f in FIELDS}, "after": change},
                )

    @staticmethod
    def event(db, fact_id, action, actor, detail):
        db.execute(
            "INSERT INTO events(fact_id,action,actor,at,detail) VALUES(?,?,?,?,?)",
            (fact_id, action, actor, now(), json.dumps(detail, ensure_ascii=False)),
        )

    def publish(self, reviewer):
        if not reviewer.strip():
            raise ValueError("A reviewer is required")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = [
                dict(r)
                for r in db.execute(
                    """SELECT f.*,s.dataset_id FROM facts f JOIN sources s ON s.id=f.source_id
                WHERE f.status='approved' AND s.active=1 ORDER BY f.created_at"""
                )
                if r["dataset_id"] in self.dataset_ids
            ]
            existing = [
                dict(r)
                for r in db.execute(
                    """SELECT f.*,s.dataset_id FROM facts f JOIN sources s ON s.id=f.source_id
                WHERE f.status='published' AND s.active=1"""
                )
                if r["dataset_id"] in self.dataset_ids
            ]
            from .reasoning import derivation_info, usable

            for row in rows:
                from .evidence import inspect

                inspect(self, row["source_id"], row["chunk_id"])
                row["source_active"] = True
                if (
                    derivation_info(self, db, row)["kind"] == "derived"
                    and not usable(self, db, row)[0]
                ):
                    raise ValueError(
                        "Derived premises are no longer valid; rederive and approve before publishing"
                    )
            existing = [
                r
                for r in existing
                if derivation_info(self, db, {**r, "source_active": True})["kind"]
                == "direct"
                or usable(self, db, {**r, "source_active": True})[0]
            ]
            for row in rows:
                for other in existing:
                    if (row["entity"], row["attribute"], row["conditions"]) == (
                        other["entity"],
                        other["attribute"],
                        other["conditions"],
                    ) and overlaps(row, other):
                        if (row["value"], row["unit"]) != (
                            other["value"],
                            other["unit"],
                        ):
                            raise ValueError(
                                f"Conflicting facts {row['id']} and {other['id']}; retire the obsolete fact explicitly before publishing"
                            )
                existing.append(row)
            for row in rows:
                db.execute(
                    "UPDATE facts SET status='published',published_at=? WHERE id=?",
                    (now(), row["id"]),
                )
                self.event(db, row["id"], "publish", reviewer, {})
            return len(rows)

    def retire(self, fact_id, reviewer, reason):
        if not reviewer.strip() or not reason.strip():
            raise ValueError("Reviewer and retirement reason are required")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT f.*,s.dataset_id FROM facts f JOIN sources s ON s.id=f.source_id WHERE f.id=?",
                (fact_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Unknown fact")
            self.require_dataset(row["dataset_id"])
            db.execute("UPDATE facts SET status='retired' WHERE id=?", (fact_id,))
            self.event(db, fact_id, "retire", reviewer, {"reason": reason})

    def verified(self, entity="", attribute="", for_external=True, on_date=None):
        date = on_date or dt.date.today().isoformat()
        dt.date.fromisoformat(date)
        with self.connect() as db:
            rows = db.execute(
                """SELECT f.*,s.name,s.dataset_id,s.document_id,s.file_hash,s.active AS source_active,c.positions
                FROM facts f JOIN sources s ON s.id=f.source_id
                JOIN chunks c ON c.source_id=f.source_id AND c.id=f.chunk_id
                WHERE f.status='published' AND s.active=1
                AND (?='' OR f.entity=?) AND (?='' OR f.attribute=?)
                AND (?=0 OR f.external_use=1)
                AND (f.valid_from='' OR f.valid_from<=?) AND (f.valid_until='' OR f.valid_until>=?)
                ORDER BY f.entity,f.attribute,f.published_at""",
                (entity, entity, attribute, attribute, for_external, date, date),
            )
            from .reasoning import usable, revision, derivation_info

            result = [
                dict(row)
                for row in rows
                if row["dataset_id"] in self.dataset_ids
                and usable(self, db, dict(row), date, for_external)[0]
            ]
            for fact in result:
                fact["revision"] = revision(fact)
                fact["origin"] = derivation_info(self, db, fact, date)
            # Advisory checks and unapproved suggestions are exposed only through review tools.
            return result

    def facts_page(
        self, status="", query="", offset=0, limit=100, include_inactive=False
    ):
        if (
            status
            not in ("", "pending", "approved", "published", "rejected", "retired")
            or not 1 <= limit <= 200
            or offset < 0
        ):
            raise ValueError("Invalid fact filters")
        scope = sorted(self.dataset_ids)
        placeholders = ",".join("?" for _ in scope)
        where = f"s.dataset_id IN ({placeholders}) AND (?='' OR f.status=?) AND (?=1 OR s.active=1) AND (?='' OR instr(f.entity,?)>0 OR instr(f.attribute,?)>0 OR instr(f.value,?)>0)"
        args = (*scope, status, status, include_inactive, query, query, query, query)
        with self.connect() as db:
            total = db.execute(
                "SELECT COUNT(*) FROM facts f JOIN sources s ON s.id=f.source_id WHERE "
                + where,
                args,
            ).fetchone()[0]
            rows = [
                dict(r)
                for r in db.execute(
                    "SELECT f.*,s.name,s.file_hash,s.active AS source_active,s.dataset_id,c.positions FROM facts f JOIN sources s ON s.id=f.source_id JOIN chunks c ON c.source_id=f.source_id AND c.id=f.chunk_id WHERE "
                    + where
                    + " ORDER BY f.created_at,f.id LIMIT ? OFFSET ?",
                    (*args, limit, offset),
                )
            ]
            from .reasoning import decorate

            rows = decorate(self, db, rows)
        return {"total": total, "rows": rows}

    def statistics(self):
        scope = sorted(self.dataset_ids)
        with self.connect() as db:
            counts = dict(
                db.execute(
                    "SELECT f.status,COUNT(*) FROM facts f JOIN sources s ON s.id=f.source_id WHERE s.active=1 AND s.dataset_id IN ("
                    + ",".join("?" for _ in scope)
                    + ") GROUP BY f.status",
                    scope,
                ).fetchall()
            )
        return {
            "sources": len(self.sources()),
            **{
                s: counts.get(s, 0)
                for s in ("pending", "approved", "published", "rejected", "retired")
            },
        }

    def audit(self, fact_id):
        with self.connect() as db:
            row = db.execute(
                "SELECT s.dataset_id FROM facts f JOIN sources s ON s.id=f.source_id WHERE f.id=?",
                (fact_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Unknown fact")
            self.require_dataset(row["dataset_id"])
            return [
                dict(r)
                for r in db.execute(
                    "SELECT action,actor,at,detail FROM events WHERE fact_id=? ORDER BY id",
                    (fact_id,),
                )
            ]

    def search_local(self, query, limit=8):
        if not query.strip() or not 1 <= limit <= 20:
            raise ValueError("Provide a query and limit between 1 and 20")
        with self.connect() as db:
            rows = db.execute(
                "SELECT s.id AS source_id,s.name AS source_name,s.file_hash AS file_sha256,c.id AS chunk_id,c.content,c.positions FROM sources s JOIN chunks c ON c.source_id=s.id WHERE s.active=1 AND s.dataset_id='local' AND instr(c.content,?)>0 LIMIT ?",
                (query, limit),
            )
            return [
                {
                    **dict(r),
                    "positions": json.loads(r["positions"]),
                    "review_status": "evidence_only",
                }
                for r in rows
            ]

    def fact(self, fact_id):
        from .reasoning import fact_row, decorate

        with self.connect() as db:
            return decorate(self, db, [fact_row(self, db, fact_id)])[0]

    def approved_facts(self):
        from .reasoning import approved_rows, decorate

        with self.connect() as db:
            return decorate(self, db, approved_rows(self, db))

    def save_check(self, fact_id, verdict, summary, checker, **kwargs):
        from .reasoning import save_check

        return save_check(self, fact_id, verdict, summary, checker, **kwargs)

    def propose_derived(self, premise_ids, reasoning, **kwargs):
        from .reasoning import propose_derived

        return propose_derived(self, premise_ids, reasoning, **kwargs)
