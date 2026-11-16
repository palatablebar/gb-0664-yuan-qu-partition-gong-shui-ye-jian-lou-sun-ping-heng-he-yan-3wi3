import csv
import io
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from app.config import RULE_VERSION, SAMPLE_DIR
from app.errors import reject
from app.store import connect
from app.modules.m1_intake.rules import read as read_csv
from app.modules.m2_contract.rules import deduplicate, normalize
from app.modules.m3_model.rules import match, validate_catalog
from app.modules.m8_night.rules import (
    apply_rows,
    local_time,
    before_due,
    backfill_key,
    backfill_record,
    basis_digest,
    compare_zones,
    due_at,
    fmt,
    impact_against,
    local_night_date,
    night_window,
    status_of,
    totals,
    validate_night_id,
    zone_balances,
    zone_touch,
)

FAULT_HOOK = None


def set_fault_hook(hook):
    """测试注入：写入完成后、提交前触发，验证发布失败会回滚旧版。"""
    global FAULT_HOOK
    FAULT_HOOK = hook


SCHEMA = """
CREATE TABLE IF NOT EXISTS night_publications (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 night_id TEXT NOT NULL,
 revision INTEGER NOT NULL,
 kind TEXT NOT NULL,
 run_id TEXT NOT NULL,
 run_sha TEXT NOT NULL,
 threshold TEXT NOT NULL,
 rule_version TEXT NOT NULL,
 basis_sha TEXT NOT NULL,
 basis TEXT NOT NULL,
 totals TEXT NOT NULL,
 base TEXT NOT NULL,
 rows TEXT NOT NULL,
 superseded TEXT NOT NULL,
 carry_in TEXT NOT NULL,
 created_at TEXT NOT NULL,
 UNIQUE(night_id, revision)
);
CREATE TABLE IF NOT EXISTS night_backfills (
 key TEXT PRIMARY KEY,
 night_id TEXT NOT NULL,
 received_at TEXT NOT NULL,
 payload TEXT NOT NULL,
 raw_text TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending',
 applied_publication INTEGER,
 carry_publication INTEGER,
 decision_at TEXT
);
CREATE TABLE IF NOT EXISTS night_state (
 night_id TEXT PRIMARY KEY,
 carry_in TEXT NOT NULL DEFAULT '[]',
 carry_out TEXT NOT NULL DEFAULT '[]',
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS night_decisions (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 night_id TEXT NOT NULL,
 backfill_key TEXT,
 action TEXT NOT NULL,
 publication INTEGER,
 note TEXT,
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS night_backfills_night ON night_backfills(night_id);
"""


def _ensure(c):
    c.executescript(SCHEMA)


def _lock(c):
    """写事务串行化：同一补录的并发重试也只产生一条修订。"""
    c.execute("BEGIN IMMEDIATE")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _threshold(value):
    try:
        threshold = Decimal(str(value if value is not None else "5"))
    except InvalidOperation:
        reject("THRESHOLD", "阈值须为非负数")
    if not threshold.is_finite() or threshold < 0:
        reject("THRESHOLD", "阈值须为非负数")
    return format(threshold.normalize(), "f")


def catalog():
    return json.loads((SAMPLE_DIR / "catalog.json").read_text())


def zones():
    return validate_catalog(catalog())


def available_nights(run_result=None):
    nights = set()
    for row in (run_result or {}).get("rows", []):
        night_id = local_night_date(row["at"])
        if night_id:
            nights.add(night_id)
    with connect() as c:
        _ensure(c)
        for table in ("night_publications", "night_backfills", "night_state"):
            for row in c.execute(f"SELECT DISTINCT night_id FROM {table}"):
                nights.add(row["night_id"])
    return sorted(nights, reverse=True)


def latest_night(run_result=None):
    nights = available_nights(run_result)
    return nights[0] if nights else None


def rows_of_night(run_result, night_id):
    return [dict(row) for row in (run_result or {}).get("rows", []) if local_night_date(row["at"]) == night_id]


def _official(c, night_id, revision=None):
    if revision is None:
        row = c.execute(
            "SELECT * FROM night_publications WHERE night_id=? ORDER BY revision DESC LIMIT 1", (night_id,)
        ).fetchone()
    else:
        row = c.execute(
            "SELECT * FROM night_publications WHERE night_id=? AND revision=?", (night_id, revision)
        ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "night_id": row["night_id"],
        "revision": row["revision"],
        "kind": row["kind"],
        "run_id": row["run_id"],
        "run_sha": row["run_sha"],
        "threshold": row["threshold"],
        "rule_version": row["rule_version"],
        "basis_sha": row["basis_sha"],
        "basis": json.loads(row["basis"]),
        "totals": json.loads(row["totals"]),
        "base": json.loads(row["base"]),
        "rows": json.loads(row["rows"]),
        "superseded": json.loads(row["superseded"]),
        "carry_in": json.loads(row["carry_in"]),
        "created_at": row["created_at"],
    }


def _state(c, night_id, create=False):
    row = c.execute("SELECT * FROM night_state WHERE night_id=?", (night_id,)).fetchone()
    if row is None:
        if not create:
            return {"night_id": night_id, "carry_in": [], "carry_out": [], "updated_at": None}
        now = _now()
        c.execute(
            "INSERT INTO night_state(night_id,carry_in,carry_out,updated_at) VALUES(?,?,?,?)",
            (night_id, "[]", "[]", now),
        )
        return {"night_id": night_id, "carry_in": [], "carry_out": [], "updated_at": now}
    return {
        "night_id": row["night_id"],
        "carry_in": json.loads(row["carry_in"]),
        "carry_out": json.loads(row["carry_out"]),
        "updated_at": row["updated_at"],
    }


def _backfills(c, night_id):
    rows = c.execute(
        "SELECT * FROM night_backfills WHERE night_id=? ORDER BY received_at, key", (night_id,)
    ).fetchall()
    result = []
    for row in rows:
        payload = json.loads(row["payload"])
        result.append(
            {
                "key": row["key"],
                "night_id": row["night_id"],
                "received_at": row["received_at"],
                "rows": payload["rows"],
                "raw_text": row["raw_text"],
                "state": row["state"],
                "applied_publication": row["applied_publication"],
                "carry_publication": row["carry_publication"],
                "decision_at": row["decision_at"],
            }
        )
    return result


def _backfill_object(backfill):
    return {"key": backfill["key"], "received_at": backfill["received_at"], "rows": backfill["rows"]}


def _history(c, night_id):
    rows = c.execute(
        "SELECT revision,kind,created_at,basis_sha,basis,totals,run_id FROM night_publications WHERE night_id=? ORDER BY revision",
        (night_id,),
    ).fetchall()
    return [
        {
            "revision": row["revision"],
            "kind": row["kind"],
            "kind_label": {"publish": "正式发布", "reopen": "重开修订"}.get(row["kind"], row["kind"]),
            "created_at": row["created_at"],
            "created_label": local_time(row["created_at"]),
            "basis_sha": row["basis_sha"],
            "adopted": json.loads(row["basis"])["labels"]["adopted"],
            "totals": json.loads(row["totals"]),
            "run_id": row["run_id"],
        }
        for row in rows
    ]


def _decisions(c, night_id):
    rows = c.execute(
        "SELECT action,backfill_key,publication,note,created_at FROM night_decisions WHERE night_id=? ORDER BY id",
        (night_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def validate_backfill_text(night_id, text):
    if not isinstance(text, str) or not text.strip():
        reject("BACKFILL", "补录内容为空")
    parsed = read_csv(text.encode("utf-8"))
    normalized = []
    problems = []
    for raw in parsed["rows"]:
        try:
            normalized.append({**normalize(raw), "line": raw["line"]})
        except Exception as exc:
            problems.append({"line": raw["line"], "code": getattr(exc, "code", "VALUE"), "message": str(exc)})
    for item in parsed["quarantine"]:
        problems.append({"line": item["line"], "code": item["code"], "message": "结构不符"})
    if problems:
        reject("BACKFILL_INVALID", "补录存在无效行：" + _json(problems[:5]))
    good, bad = deduplicate(normalized)
    if bad:
        reject("BACKFILL_INVALID", "补录存在重复或冲突记录")
    book = catalog()
    rows = []
    for row in good:
        if not match(row, book):
            reject("BACKFILL_INVALID", f"第{row['line']}行计量关系不存在")
        if local_night_date(row["at"]) != night_id:
            reject("NIGHT_MISMATCH", f"第{row['line']}行不属于夜次 {night_id}")
        rows.append(row)
    if not rows:
        reject("BACKFILL", "补录没有可用记录")
    return rows


def submit_backfill(night_id, text, received_at=None):
    """补录入口：逻辑读数完全相同的重试只保留一条，不重复计入。"""
    validate_night_id(night_id)
    rows = validate_backfill_text(night_id, text)
    received_at = received_at or _now()
    key = backfill_key(night_id, rows)
    payload = {"night_id": night_id, "received_at": received_at, "rows": rows}
    with connect() as c:
        _ensure(c)
        _lock(c)
        existing = c.execute("SELECT * FROM night_backfills WHERE key=?", (key,)).fetchone()
        if existing:
            return {
                "key": key,
                "night_id": night_id,
                "state": existing["state"],
                "received_at": existing["received_at"],
                "duplicate": True,
                "created": False,
                "record_rows": backfill_record(rows),
            }
        try:
            c.execute(
                "INSERT INTO night_backfills(key,night_id,received_at,payload,raw_text,state) VALUES(?,?,?,?,?,?)",
                (key, night_id, received_at, _json(payload), text, "pending"),
            )
            c.execute(
                "INSERT INTO night_decisions(night_id,backfill_key,action,created_at) VALUES(?,?,?,?)",
                (night_id, key, "received", _now()),
            )
        except sqlite3.IntegrityError:
            c.rollback()
            return {
                "key": key,
                "night_id": night_id,
                "state": c.execute("SELECT state FROM night_backfills WHERE key=?", (key,)).fetchone()["state"],
                "received_at": received_at,
                "duplicate": True,
                "created": False,
                "record_rows": backfill_record(rows),
            }
    return {
        "key": key,
        "night_id": night_id,
        "state": "pending",
        "received_at": received_at,
        "duplicate": False,
        "created": True,
        "record_rows": backfill_record(rows),
    }


def _write_publication(c, night_id, revision, kind, run_id, run_sha, threshold, basis, total, base, rows, superseded, carry_in):
    created_at = _now()
    cursor = c.execute(
        "INSERT INTO night_publications(night_id,revision,kind,run_id,run_sha,threshold,rule_version,basis_sha,basis,totals,base,rows,superseded,carry_in,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            night_id,
            revision,
            kind,
            run_id,
            run_sha,
            threshold,
            RULE_VERSION,
            basis["sha"],
            _json(basis),
            _json(total),
            _json(base),
            _json(rows),
            _json(superseded),
            _json(carry_in),
            created_at,
        ),
    )
    publication = cursor.lastrowid
    check = c.execute("SELECT revision FROM night_publications WHERE id=?", (publication,)).fetchone()
    if check is None or check["revision"] != revision:
        raise RuntimeError("发布写入校验失败")
    return publication, created_at


def _basis(night_id, run_sha, threshold, applied_keys, carry_keys):
    basis = {"sha": basis_digest(night_id, run_sha, threshold, RULE_VERSION, applied_keys, carry_keys)}
    basis["labels"] = {
        "run": run_sha,
        "threshold": threshold,
        "rule": RULE_VERSION,
        "adopted": applied_keys,
        "carried_in": carry_keys,
    }
    return basis


def publish(night_id, run_result):
    """17:00 后发布前晚结论。重复发布不新增修订；写入失败整体回滚，旧版继续有效。"""
    validate_night_id(night_id)
    if run_result is None:
        reject("RUN", "缺少运行结果")
    manifest = run_result.get("manifest") or {}
    run_sha = manifest.get("sha256")
    if not run_sha:
        reject("RUN", "运行缺少摘要")
    run_id = manifest.get("run_id")
    threshold = _threshold((run_result.get("params") or {}).get("threshold"))
    if before_due(night_id):
        reject("TOO_EARLY", f"夜次 {night_id} 须在 {due_at(night_id)} 后发布")
    with connect() as c:
        _ensure(c)
        _lock(c)
        official = _official(c, night_id)
        if official is not None:
            pending = [b for b in _backfills(c, night_id) if b["state"] == "pending"]
            if pending:
                reject("PENDING_DECISION", "已有迟到补录，请先选择重开当班或移交下一班")
            if official["run_sha"] != run_sha:
                reject("ALREADY_PUBLISHED", "该夜次已有正式结论；换运行重算须通过补录重开")
            c.commit()
            return {"created": False, "duplicate": True, "detail": night_detail(night_id, run_result)}
        base = rows_of_night(run_result, night_id)
        if not base:
            reject("NIGHT_EMPTY", f"夜次 {night_id} 没有可发布读数")
        pending = [b for b in _backfills(c, night_id) if b["state"] == "pending"]
        merged, superseded = apply_rows(base, pending)
        applied_keys = [b["key"] for b in pending]
        state = _state(c, night_id, create=True)
        carry_keys = [item["backfill_key"] for item in state["carry_in"]]
        basis = _basis(night_id, run_sha, threshold, applied_keys, carry_keys)
        total = totals(zone_balances(merged, zones(), threshold))
        publication, created_at = _write_publication(
            c, night_id, 1, "publish", run_id, run_sha, threshold, basis, total, base, merged, superseded, state["carry_in"]
        )
        for backfill in pending:
            c.execute(
                "UPDATE night_backfills SET state='applied',applied_publication=?,decision_at=? WHERE key=?",
                (publication, created_at, backfill["key"]),
            )
        c.execute(
            "INSERT INTO night_decisions(night_id,action,publication,note,created_at) VALUES(?,?,?,?,?)",
            (night_id, "publish", publication, f"revision 1，采纳补录 {len(applied_keys)} 条", created_at),
        )
        if FAULT_HOOK:
            FAULT_HOOK("publish")
        try:
            c.commit()
        except sqlite3.IntegrityError:
            c.rollback()
            return {"created": False, "duplicate": True, "detail": night_detail(night_id, run_result)}
    return {"created": True, "duplicate": False, "detail": night_detail(night_id, run_result)}


def reopen(night_id, key, run_result=None):
    """重开当班结论：补录并入新修订；同一补录重试不新增修订、不重复计入。"""
    validate_night_id(night_id)
    with connect() as c:
        _ensure(c)
        _lock(c)
        official = _official(c, night_id)
        if official is None:
            reject("NOT_PUBLISHED", "当班尚未发布，补录随首次发布生效")
        backfill = c.execute(
            "SELECT * FROM night_backfills WHERE key=? AND night_id=?", (key, night_id)
        ).fetchone()
        if backfill is None:
            reject("BACKFILL", "补录不存在")
        if backfill["state"] == "carried":
            reject("BACKFILL_CARRIED", "补录已移交下一班，不能重开本班")
        existing = _backfills(c, night_id)
        adopted = [b for b in existing if b["state"] == "applied"]
        chosen = next(b for b in existing if b["key"] == key)
        if not any(b["key"] == key for b in adopted):
            adopted.append(chosen)
        merged, superseded = apply_rows(official["base"], adopted)
        applied_keys = [b["key"] for b in adopted]
        carry_keys = [item["backfill_key"] for item in official["carry_in"]]
        basis = _basis(night_id, official["run_sha"], official["threshold"], applied_keys, carry_keys)
        unchanged = [
            {k: v for k, v in row.items() if k not in ("line", "original_line", "origin", "backfill_key")}
            for row in merged
        ] == [
            {k: v for k, v in row.items() if k not in ("line", "original_line", "origin", "backfill_key")}
            for row in official["rows"]
        ]
        if basis["sha"] == official["basis_sha"] or unchanged:
            c.commit()
            return {"created": False, "duplicate": True, "detail": night_detail(night_id, run_result)}
        revision = official["revision"] + 1
        total = totals(zone_balances(merged, zones(), official["threshold"]))
        publication, created_at = _write_publication(
            c,
            night_id,
            revision,
            "reopen",
            official["run_id"],
            official["run_sha"],
            official["threshold"],
            basis,
            total,
            official["base"],
            merged,
            superseded,
            official["carry_in"],
        )
        c.execute(
            "UPDATE night_backfills SET state='applied',applied_publication=?,decision_at=? WHERE key=?",
            (publication, created_at, key),
        )
        c.execute(
            "INSERT INTO night_decisions(night_id,backfill_key,action,publication,note,created_at) VALUES(?,?,?,?,?,?)",
            (night_id, key, "reopen", publication, f"revision {revision}", created_at),
        )
        if FAULT_HOOK:
            FAULT_HOOK("reopen")
        try:
            c.commit()
        except sqlite3.IntegrityError:
            c.rollback()
            return {"created": False, "duplicate": True, "detail": night_detail(night_id, run_result)}
    return {"created": True, "duplicate": False, "detail": night_detail(night_id, run_result)}


def carry(night_id, key, note=None):
    """保留当班正式结论，把补录影响移交下一班：不新增修订、不改本期净水量。"""
    validate_night_id(night_id)
    with connect() as c:
        _ensure(c)
        _lock(c)
        official = _official(c, night_id)
        if official is None:
            reject("NOT_PUBLISHED", "当班尚未发布，补录随首次发布生效")
        backfill = c.execute(
            "SELECT * FROM night_backfills WHERE key=? AND night_id=?", (key, night_id)
        ).fetchone()
        if backfill is None:
            reject("BACKFILL", "补录不存在")
        if backfill["state"] == "carried":
            c.commit()
            return {"created": False, "duplicate": True, "detail": night_detail(night_id)}
        if backfill["state"] == "applied":
            reject("BACKFILL_APPLIED", "补录已并入本班修订，不能再移交；如需更正请重开")
        payload = json.loads(backfill["payload"])
        base_items = zone_balances(official["rows"], zones(), official["threshold"])
        after, _ = apply_rows(official["rows"], [{"key": key, "received_at": payload["received_at"], "rows": payload["rows"]}])
        after_items = zone_balances(after, zones(), official["threshold"])
        changes = impact_against(base_items, after_items)
        next_id = _next_night(night_id)
        state_next = _state(c, next_id, create=True)
        now = _now()
        carry_item = {
            "from_night": night_id,
            "backfill_key": key,
            "received_at": payload["received_at"],
            "records": backfill_record(payload["rows"]),
            "impact": changes,
            "totals_before": totals(base_items),
            "totals_after": totals(after_items),
            "note": note,
            "created_at": now,
        }
        carry_in = [x for x in state_next["carry_in"] if x["backfill_key"] != key] + [carry_item]
        c.execute(
            "UPDATE night_state SET carry_in=?,updated_at=? WHERE night_id=?",
            (_json(carry_in), now, next_id),
        )
        state = _state(c, night_id)
        carry_out = [x for x in state["carry_out"] if x["backfill_key"] != key] + [
            {"to_night": next_id, "backfill_key": key, "impact": changes, "note": note, "created_at": now}
        ]
        c.execute(
            "UPDATE night_state SET carry_out=?,updated_at=? WHERE night_id=?",
            (_json(carry_out), now, night_id),
        )
        c.execute(
            "UPDATE night_backfills SET state='carried',carry_publication=?,decision_at=? WHERE key=?",
            (official["id"], now, key),
        )
        c.execute(
            "INSERT INTO night_decisions(night_id,backfill_key,action,publication,note,created_at) VALUES(?,?,?,?,?,?)",
            (night_id, key, "carry", official["id"], note or f"移交 {next_id}", now),
        )
        if FAULT_HOOK:
            FAULT_HOOK("carry")
        c.commit()
    return {"created": True, "duplicate": False, "detail": night_detail(night_id)}


def _next_night(night_id):
    return (datetime.strptime(night_id, "%Y-%m-%d").date() + timedelta(days=1)).isoformat()


def _backfill_view(backfill, base, threshold, adopted):
    """每条补录的补录前后对照：只叠加本条，其余已采纳补录作为基线。"""
    others = [b for b in adopted if b["key"] != backfill["key"]]
    before_rows, _ = apply_rows(base, others)
    after_rows, superseded = apply_rows(base, others + [_backfill_object(backfill)])
    before_items = zone_balances(before_rows, zones(), threshold)
    after_items = zone_balances(after_rows, zones(), threshold)
    adopted_ids = {row["record_id"] for row in before_rows}
    existing = [row for row in backfill["rows"] if row["record_id"] in adopted_ids]
    return {
        "key": backfill["key"],
        "night_id": backfill["night_id"],
        "received_at": backfill["received_at"],
        "received_label": local_time(backfill["received_at"]),
        "included_at": local_time(backfill["decision_at"]),
        "state": backfill["state"],
        "applied_publication": backfill["applied_publication"],
        "carry_publication": backfill["carry_publication"],
        "decision_at": backfill["decision_at"],
        "record_rows": backfill_record(backfill["rows"]),
        "touches": zone_touch(backfill["rows"]),
        "impact": impact_against(before_items, after_items),
        "totals_before": totals(before_items),
        "totals_after": totals(after_items),
        "replaces": backfill_record(existing),
        "adds": backfill_record([row for row in backfill["rows"] if row["record_id"] not in adopted_ids]),
        "superseded": superseded,
        "raw_text": backfill["raw_text"],
    }


def night_detail(night_id, run_result=None, run_id=None):
    validate_night_id(night_id)
    with connect() as c:
        _ensure(c)
        official = _official(c, night_id)
        first = _official(c, night_id, 1)
        state = _state(c, night_id)
        backfills = _backfills(c, night_id)
        history = _history(c, night_id)
        decisions = _decisions(c, night_id)
    threshold = official["threshold"] if official else _threshold(((run_result or {}).get("params") or {}).get("threshold"))
    base = official["base"] if official else rows_of_night(run_result, night_id)
    base_items = zone_balances(base, zones(), threshold)
    active = [b for b in backfills if b["state"] in ("pending", "applied")]
    merged, superseded = apply_rows(base, active)
    preview = zone_balances(merged, zones(), threshold)
    first_items = zone_balances(first["rows"], zones(), first["threshold"]) if first else None
    current_items = zone_balances(official["rows"], zones(), official["threshold"]) if official else base_items
    comparison = compare_zones(
        first_items, current_items, preview, [b for b in backfills if b["state"] == "pending"]
    )
    adopted = [b for b in backfills if b["state"] == "applied"]
    backfill_views = [_backfill_view(b, base, threshold, adopted) for b in backfills]
    pending = [b for b in backfill_views if b["state"] == "pending"]
    status, status_label = status_of({"pending": bool(pending), "latest": official, "carry_out": state["carry_out"]})
    return {
        "night_id": night_id,
        "due_at": due_at(night_id),
        "window": night_window(night_id),
        "threshold": threshold,
        "rule_version": RULE_VERSION,
        "published": official is not None,
        "official": _publication_view(official, current_items) if official else None,
        "official_zones": zone_balances(official["rows"], zones(), official["threshold"]) if official else None,
        "latest_revision": official["revision"] if official else None,
        "latest_basis": official["basis"] if official else None,
        "latest_totals": official["totals"] if official else None,
        "first": _publication_view(first) if first else None,
        "first_revision": first["revision"] if first else None,
        "first_zones": first_items,
        "current": current_items,
        "current_totals": totals(current_items),
        "base_totals": totals(base_items),
        "preview": preview,
        "preview_totals": totals(preview),
        "comparison": comparison,
        "backfills": backfill_views,
        "pending_count": len(pending),
        "history": history,
        "decisions": decisions,
        "carry_in": state["carry_in"],
        "carry_out": state["carry_out"],
        "status": status,
        "status_label": status_label,
        "record_count": len(base),
        "superseded_count": len(superseded),
        "run_id": official["run_id"] if official else run_id,
    }


def _publication_view(publication, zone_items=None):
    if publication is None:
        return None
    return {
        "revision": publication["revision"],
        "kind": publication["kind"],
        "kind_label": {"publish": "正式发布", "reopen": "重开修订"}.get(publication["kind"], publication["kind"]),
        "run_id": publication["run_id"],
        "run_sha": publication["run_sha"],
        "threshold": publication["threshold"],
        "rule_version": publication["rule_version"],
        "basis_sha": publication["basis_sha"],
        "basis": publication["basis"],
        "totals": publication["totals"],
        "created_at": publication["created_at"],
        "created_label": local_time(publication["created_at"]),
        "superseded": publication["superseded"],
        "zones": zone_items,
        "records": [
            {
                "record_id": row["record_id"],
                "meter": row.get("meter"),
                "line": row.get("line"),
                "value_m3": fmt(row["value_m3"]),
                "origin": row.get("origin", "原始运行"),
            }
            for row in publication["rows"]
        ],
    }


def readings(night_id, record_id):
    """原读数追回：返回记录在历次发布与补录中的全部取值。"""
    validate_night_id(night_id)
    if not isinstance(record_id, str) or not record_id.strip():
        reject("READING", "记录号不能为空")
    with connect() as c:
        _ensure(c)
        publications = c.execute(
            "SELECT * FROM night_publications WHERE night_id=? ORDER BY revision", (night_id,)
        ).fetchall()
        backfills = _backfills(c, night_id)
    versions = []
    for publication in publications:
        for row in json.loads(publication["rows"]):
            if row["record_id"] != record_id:
                continue
            versions.append(
                {
                    "source": "正式结论",
                    "revision": publication["revision"],
                    "kind": publication["kind"],
                    "value_m3": fmt(row["value_m3"]),
                    "line": row.get("line"),
                    "origin": row.get("origin", "原始运行"),
                    "backfill_key": row.get("backfill_key"),
                    "at": row["at"],
                    "created_at": publication["created_at"],
                }
            )
        for item in json.loads(publication["superseded"]):
            if item["record_id"] != record_id:
                continue
            versions.append(
                {
                    "source": "被替换的原读数",
                    "revision": publication["revision"],
                    "kind": publication["kind"],
                    "value_m3": fmt(item["original"]["value_m3"]),
                    "line": item["original"].get("line"),
                    "origin": "原始运行",
                    "backfill_key": item["backfill_key"],
                    "at": item["original"]["at"],
                    "created_at": item["received_at"],
                }
            )
    for backfill in backfills:
        for row in backfill["rows"]:
            if row["record_id"] != record_id:
                continue
            versions.append(
                {
                    "source": "补录",
                    "revision": None,
                    "kind": backfill["state"],
                    "value_m3": fmt(row["value_m3"]),
                    "line": row.get("line"),
                    "origin": "补录",
                    "backfill_key": backfill["key"],
                    "at": row["at"],
                    "created_at": backfill["received_at"],
                }
            )
    if not versions:
        reject("READING", "该记录不存在")
    return {"night_id": night_id, "record_id": record_id, "versions": versions}


def night_summaries():
    with connect() as c:
        _ensure(c)
        nights = {row["night_id"] for row in c.execute("SELECT night_id FROM night_publications")}
        nights |= {row["night_id"] for row in c.execute("SELECT night_id FROM night_backfills")}
        nights |= {row["night_id"] for row in c.execute("SELECT night_id FROM night_state")}
        result = []
        for night_id in sorted(nights, reverse=True):
            official = _official(c, night_id)
            state = _state(c, night_id)
            backfills = _backfills(c, night_id)
            pending = [b for b in backfills if b["state"] == "pending"]
            result.append(
                {
                    "night_id": night_id,
                    "due_at": due_at(night_id),
                    "revision": official["revision"] if official else None,
                    "kind": official["kind"] if official else None,
                    "status": status_of(
                        {"pending": bool(pending), "latest": official, "carry_out": state["carry_out"]}
                    )[0],
                    "pending": len(pending),
                    "carried": len([b for b in backfills if b["state"] == "carried"]),
                    "totals": official["totals"] if official else None,
                }
            )
        return result


def evidence_csv(night_id, run_result=None):
    detail = night_detail(night_id, run_result)
    with connect() as c:
        _ensure(c)
        publication = _official(c, night_id)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["夜次", "版本", "类别", "记录号", "表号", "来源行", "原值/m³", "现值/m³", "补录键", "采用依据"])
    if publication:
        basis = publication["basis_sha"]
        for row in publication["rows"]:
            writer.writerow(
                [night_id, publication["revision"], "正式结论", row["record_id"], row.get("meter", ""),
                 row.get("line", ""), fmt(row["value_m3"]), fmt(row["value_m3"]), row.get("backfill_key", ""), basis]
            )
        for item in publication["superseded"]:
            original = item["original"]
            writer.writerow(
                [night_id, publication["revision"], "被替换的原读数", item["record_id"], original.get("meter", ""),
                 original.get("line", ""), fmt(original["value_m3"]), "", item["backfill_key"], basis]
            )
    for backfill in detail["backfills"]:
        for row in backfill["record_rows"]:
            writer.writerow(
                [night_id, detail["latest_revision"] or "", f"补录/{backfill['state']}", row["record_id"],
                 row.get("meter", ""), row.get("line", ""), "", row["value_m3"], backfill["key"],
                 detail["official"]["basis_sha"] if detail["official"] else ""]
            )
    return "﻿" + buffer.getvalue()
