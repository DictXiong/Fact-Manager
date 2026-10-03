"""Versioned advisory checks and explicit, revocable derivation dependencies."""

from contextlib import nullcontext
import datetime as dt
import json
from fractions import Fraction
import re
import uuid

from .core import FIELDS, digest, now, overlaps, validate_fields

SCHEMA = """
CREATE TABLE IF NOT EXISTS derivations (
 fact_id TEXT PRIMARY KEY REFERENCES facts(id), rule TEXT NOT NULL,
 reasoning TEXT NOT NULL, conclusion_revision TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fact_dependencies (
 fact_id TEXT NOT NULL REFERENCES facts(id), premise_id TEXT NOT NULL REFERENCES facts(id),
 premise_revision TEXT NOT NULL, PRIMARY KEY(fact_id,premise_id)
);
CREATE TABLE IF NOT EXISTS fact_checks (
 id TEXT PRIMARY KEY, fact_id TEXT NOT NULL REFERENCES facts(id), target_revision TEXT NOT NULL,
 kind TEXT NOT NULL, checker TEXT NOT NULL, verdict TEXT NOT NULL, summary TEXT NOT NULL,
 suggestions TEXT NOT NULL, context_revision TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS checks_by_fact ON fact_checks(fact_id,created_at);
CREATE TABLE IF NOT EXISTS check_dependencies (
 check_id TEXT NOT NULL REFERENCES fact_checks(id), premise_id TEXT NOT NULL REFERENCES facts(id),
 premise_revision TEXT NOT NULL, PRIMARY KEY(check_id,premise_id)
);
"""
VERDICTS = ("supported", "needs_review", "contradicted", "insufficient_evidence")


def revision(fact):
    return digest({k: fact[k] for k in (*FIELDS, "source_id", "chunk_id", "quote")})


def fact_row(store, db, fact_id):
    row = db.execute(
        "SELECT f.*,s.name,s.dataset_id,s.document_id,s.file_hash,s.active AS source_active "
        "FROM facts f JOIN sources s ON s.id=f.source_id WHERE f.id=?",
        (fact_id,),
    ).fetchone()
    if row is None:
        raise ValueError("Unknown fact in this library")
    store.require_dataset(row["dataset_id"])
    return dict(row)


def usable(store, db, fact, date=None, external=False, path=()):
    date = date or dt.date.today().isoformat()
    if fact["id"] in path or len(path) >= 32:
        return False, "推导依赖存在循环或超过32层"
    if fact["dataset_id"] not in store.dataset_ids or not fact["source_active"]:
        return False, "来源已停用、更新或取消绑定"
    from .evidence import integrity

    original = db.execute(
        "SELECT original_path,file_hash FROM sources WHERE id=?", (fact["source_id"],)
    ).fetchone()
    if original is None or not integrity(original):
        return False, "原文件缺失或校验失败"
    if fact["status"] not in ("approved", "published"):
        return False, "前提未审批或已被淘汰"
    if (fact["valid_from"] and fact["valid_from"] > date) or (
        fact["valid_until"] and fact["valid_until"] < date
    ):
        return False, "前提不在有效期内"
    if external and not fact["external_use"]:
        return False, "前提不允许外部引用"
    derivation = db.execute(
        "SELECT * FROM derivations WHERE fact_id=?", (fact["id"],)
    ).fetchone()
    if derivation:
        if derivation["conclusion_revision"] != revision(fact):
            return False, "推导结论已改变，须重新推导和审批"
        dependencies = db.execute(
            "SELECT * FROM fact_dependencies WHERE fact_id=?", (fact["id"],)
        ).fetchall()
        if not dependencies:
            return False, "推导缺少前提"
        for dependency in dependencies:
            try:
                premise = fact_row(store, db, dependency["premise_id"])
            except ValueError:
                return False, "前提不属于当前授权范围"
            if revision(premise) != dependency["premise_revision"]:
                return False, "前提内容已改变，须重新推导和审批"
            good, reason = usable(
                store, db, premise, date, external, (*path, fact["id"])
            )
            if not good:
                return False, reason
    return True, ""


def derivation_info(store, db, fact, date=None):
    row = db.execute(
        "SELECT * FROM derivations WHERE fact_id=?", (fact["id"],)
    ).fetchone()
    if not row:
        return {"kind": "direct"}
    dependencies = []
    valid = row["conclusion_revision"] == revision(fact)
    reasons = [] if valid else ["推导结论已改变"]
    for dep in db.execute(
        "SELECT * FROM fact_dependencies WHERE fact_id=?", (fact["id"],)
    ):
        item = {"id": dep["premise_id"], "expected_revision": dep["premise_revision"]}
        try:
            premise = fact_row(store, db, dep["premise_id"])
            item.update(
                {
                    k: premise[k]
                    for k in (
                        "entity",
                        "attribute",
                        "value",
                        "unit",
                        "conditions",
                        "status",
                    )
                }
            )
            good, reason = usable(store, db, premise, date)
            if revision(premise) != dep["premise_revision"]:
                good, reason = False, "前提内容已改变"
        except ValueError:
            good, reason = False, "前提不属于当前授权范围"
        item.update(current=good, reason=reason)
        dependencies.append(item)
        if not good:
            valid = False
            reasons.append(reason)
    if not dependencies:
        valid = False
        reasons.append("缺少前提")
    return {
        "kind": "derived",
        "rule": row["rule"],
        "reasoning": row["reasoning"],
        "current": valid,
        "reasons": sorted(set(reasons)),
        "premises": dependencies,
        "certainty": (
            "规则计算，仍需审批"
            if row["rule"] == "ratio_complement"
            else "推理建议，须人工核验"
        ),
    }


def approved_rows(store, db):
    result = []
    for row in db.execute(
        "SELECT f.*,s.name,s.dataset_id,s.document_id,s.file_hash,s.active AS source_active "
        "FROM facts f JOIN sources s ON s.id=f.source_id WHERE f.status IN ('approved','published') ORDER BY f.id"
    ):
        fact = dict(row)
        if usable(store, db, fact)[0]:
            result.append(fact)
    return result


def approved_revision(store, db):
    return digest([(f["id"], revision(f)) for f in approved_rows(store, db)])


def check_info(store, db, fact, context=None):
    rows = db.execute(
        "SELECT * FROM fact_checks WHERE fact_id=? ORDER BY (kind='local'),created_at DESC,rowid DESC",
        (fact["id"],),
    ).fetchall()
    if not rows:
        return None
    check = dict(rows[0])
    reasons = []
    if check["target_revision"] != revision(fact):
        reasons.append("候选内容已改变")
    if not fact["source_active"] or fact["dataset_id"] not in store.dataset_ids:
        reasons.append("来源或授权范围已改变")
    from .evidence import integrity

    original = db.execute(
        "SELECT original_path,file_hash FROM sources WHERE id=?", (fact["source_id"],)
    ).fetchone()
    if original is None or not integrity(original):
        reasons.append("原文件缺失或校验失败")
    if check["context_revision"] and check["context_revision"] != (
        context if context is not None else approved_revision(store, db)
    ):
        reasons.append("已审批事实集合已改变，需要重新检查")
    premises = []
    for dep in db.execute(
        "SELECT * FROM check_dependencies WHERE check_id=?", (check["id"],)
    ):
        try:
            premise = fact_row(store, db, dep["premise_id"])
            valid, reason = usable(store, db, premise)
            if revision(premise) != dep["premise_revision"]:
                valid, reason = False, "前提内容已改变"
        except ValueError:
            valid, reason = False, "前提不属于当前授权范围"
        premises.append({"id": dep["premise_id"], "current": valid, "reason": reason})
        if not valid:
            reasons.append(reason)
    check.update(
        suggestions=json.loads(check["suggestions"]),
        current=not reasons,
        stale_reasons=sorted(set(reasons)),
        premises=premises,
    )
    return check


def decorate(store, db, rows):
    context = approved_revision(store, db)
    for fact in rows:
        fact["revision"] = revision(fact)
        fact["origin"] = derivation_info(store, db, fact)
        fact["check"] = check_info(store, db, fact, context)
    return rows


def save_check(
    store,
    fact_id,
    verdict,
    summary,
    checker,
    *,
    suggestions=None,
    premise_ids=None,
    kind="agent",
    expected_revision=None,
    context_revision="",
    premise_revisions=None,
    _db=None,
):
    if verdict not in VERDICTS or kind not in ("agent", "ai", "local"):
        raise ValueError("Invalid check classification")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 8000:
        raise ValueError("Provide a check summary (1–8000 characters)")
    if not isinstance(checker, str) or not checker.strip() or len(checker) > 100:
        raise ValueError("Provide the checker name")
    suggestions = suggestions or {}
    premise_ids = premise_ids or []
    if (
        not isinstance(suggestions, dict)
        or not set(suggestions) <= set(FIELDS)
        or any(not isinstance(v, str) or len(v) > 8000 for v in suggestions.values())
    ):
        raise ValueError(
            "Suggestions can only change fact fields; evidence and approval are read-only"
        )
    if (
        not isinstance(premise_ids, list)
        or len(premise_ids) > 16
        or any(not isinstance(p, str) for p in premise_ids)
        or len(set(premise_ids)) != len(premise_ids)
    ):
        raise ValueError("Provide up to 16 distinct premise IDs")
    if verdict == "contradicted" and not premise_ids:
        raise ValueError(
            "A contradiction conclusion must cite an eligible approved premise; otherwise use needs_review"
        )
    with store.connect() if _db is None else nullcontext(_db) as db:
        if _db is None:
            db.execute("BEGIN IMMEDIATE")
        fact = fact_row(store, db, fact_id)
        if not fact["source_active"] or fact["status"] not in (
            "pending",
            "approved",
            "published",
        ):
            raise ValueError("Cannot check an inactive, rejected or retired fact")
        current_revision = revision(fact)
        if not expected_revision or expected_revision != current_revision:
            raise ValueError(
                "Check target changed; read its current revision before submitting"
            )
        if context_revision and context_revision != approved_revision(store, db):
            raise ValueError("Approved facts changed during checking; retry")
        from .evidence import inspect

        inspect(store, fact["source_id"], fact["chunk_id"])
        validate_fields({**fact, **suggestions})
        dependencies = []
        for pid in premise_ids:
            if pid == fact_id:
                raise ValueError("A fact cannot be its own check premise")
            premise = fact_row(store, db, pid)
            if not usable(store, db, premise)[0]:
                raise ValueError("Check premises must be approved, current and valid")
            if not isinstance(premise_revisions, dict) or premise_revisions.get(
                pid
            ) != revision(premise):
                raise ValueError("Premise changed; read its revision before submitting")
            dependencies.append((pid, revision(premise)))
        if verdict == "contradicted":
            relevant = [fact_row(store, db, pid) for pid in premise_ids]
            if not any(
                (p["entity"], p["attribute"], p["conditions"])
                == (fact["entity"], fact["attribute"], fact["conditions"])
                and overlaps(p, fact)
                and (p["value"], p["unit"]) != (fact["value"], fact["unit"])
                for p in relevant
            ):
                raise ValueError(
                    "Contradiction requires a different value for the same entity, attribute, exact conditions and overlapping dates; otherwise use needs_review"
                )
        cid = uuid.uuid4().hex
        db.execute(
            "INSERT INTO fact_checks VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                cid,
                fact_id,
                current_revision,
                kind,
                checker.strip(),
                verdict,
                summary.strip(),
                json.dumps(suggestions, ensure_ascii=False),
                context_revision,
                now(),
            ),
        )
        db.executemany(
            "INSERT INTO check_dependencies VALUES (?,?,?)",
            [(cid, *dep) for dep in dependencies],
        )
        store.event(
            db,
            fact_id,
            "check",
            checker,
            {
                "check_id": cid,
                "verdict": verdict,
                "summary": summary,
                "premise_ids": premise_ids,
                "suggestions": suggestions,
                "approval": "unchanged",
            },
        )
        return check_info(store, db, fact)


def propose_derived(
    store,
    premise_ids,
    reasoning,
    *,
    rule="reasoned",
    candidate=None,
    actor="管理员",
    premise_revisions=None,
):
    if (
        rule not in ("reasoned", "ratio_complement")
        or not isinstance(reasoning, str)
        or not reasoning.strip()
        or len(reasoning) > 12000
    ):
        raise ValueError("Provide a supported rule and a bounded explanation")
    if (
        not isinstance(premise_ids, list)
        or not 1 <= len(premise_ids) <= 16
        or any(not isinstance(p, str) for p in premise_ids)
        or len(set(premise_ids)) != len(premise_ids)
    ):
        raise ValueError("Provide 1–16 distinct approved premise IDs")
    if not isinstance(actor, str) or not actor.strip() or len(actor) > 100:
        raise ValueError("Provide the proposer name")
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        premises = [fact_row(store, db, p) for p in premise_ids]
        if any(not usable(store, db, p)[0] for p in premises):
            raise ValueError(
                "All derivation premises must be approved, current and valid"
            )
        if not isinstance(premise_revisions, dict) or any(
            premise_revisions.get(p["id"]) != revision(p) for p in premises
        ):
            raise ValueError(
                "Premise changed; read each current revision before proposing"
            )
        anchor = premises[0]
        start = max((p["valid_from"] for p in premises), default="")
        ends = [p["valid_until"] for p in premises if p["valid_until"]]
        end = min(ends) if ends else ""
        if end and start and end < start:
            raise ValueError("Premises have no overlapping validity window")
        if rule == "ratio_complement":
            if (
                len(premises) != 1
                or not anchor["attribute"].endswith("比例")
                or anchor["unit"] not in ("", "比例")
                or not re.fullmatch(r"\d+(?:/\d+|\.\d+)?", anchor["value"])
            ):
                raise ValueError(
                    "Exact ratio rule requires one dimensionless, unqualified ratio premise whose attribute ends with 比例"
                )
            try:
                ratio = Fraction(anchor["value"])
            except (ValueError, ZeroDivisionError):
                raise ValueError("Invalid ratio") from None
            if not 0 <= ratio <= 1:
                raise ValueError("Ratio must be between zero and one")
            result = 1 - ratio
            value = (
                str(result.numerator)
                if result.denominator == 1
                else f"{result.numerator}/{result.denominator}"
            )
            values = {
                **{k: anchor[k] for k in FIELDS},
                "attribute": anchor["attribute"][:-2] + "补比例",
                "value": value,
                "unit": "",
                "valid_from": start,
                "valid_until": end,
            }
            reasoning = (
                f'同一主体、计量对象和条件的数学补量：补比例 = 1 - ({anchor["value"]}) = {value}。补比例不自动表示减少率、成本节省或性能提升，其业务含义仍须核对；不推及其他对象或测试场景。\n'
                + reasoning.strip()
            )
        else:
            if not isinstance(candidate, dict) or not set(candidate) <= set(FIELDS):
                raise ValueError("Supply conclusion fields for a reasoned proposal")
            values = {k: candidate.get(k, "") for k in FIELDS}
            validate_fields(values)
            values["valid_from"] = max(values["valid_from"], start)
            values["valid_until"] = (
                min(v for v in (values["valid_until"], end) if v)
                if values["valid_until"] or end
                else ""
            )
        validate_fields(values)
        fingerprint = digest(
            [
                "derived",
                values,
                rule,
                reasoning,
                [(p["id"], revision(p)) for p in premises],
            ]
        )
        existing = db.execute(
            "SELECT id FROM facts WHERE fingerprint=?", (fingerprint,)
        ).fetchone()
        if existing:
            return decorate(store, db, [fact_row(store, db, existing["id"])])[0]
        fid = uuid.uuid4().hex
        db.execute(
            "INSERT INTO facts (id,source_id,chunk_id,entity,attribute,value,unit,conditions,valid_from,valid_until,quote,status,fingerprint,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,'pending',?,?)",
            (
                fid,
                anchor["source_id"],
                anchor["chunk_id"],
                *(values[k] for k in FIELDS),
                anchor["quote"],
                fingerprint,
                now(),
            ),
        )
        fact = fact_row(store, db, fid)
        db.execute(
            "INSERT INTO derivations VALUES (?,?,?,?,?)",
            (fid, rule, reasoning, revision(fact), now()),
        )
        db.executemany(
            "INSERT INTO fact_dependencies VALUES (?,?,?)",
            [(fid, p["id"], revision(p)) for p in premises],
        )
        store.event(
            db,
            fid,
            "derive",
            actor,
            {
                "rule": rule,
                "reasoning": reasoning,
                "premise_ids": premise_ids,
                "approval": "pending",
                "external_use": False,
            },
        )
        return decorate(store, db, [fact])[0]
