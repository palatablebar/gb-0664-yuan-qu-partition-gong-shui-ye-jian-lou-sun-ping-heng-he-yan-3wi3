import copy
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.errors import reject
from app.modules.m4_calculate.rules import balances
from app.modules.m6_reconcile.rules import classify

CST = timezone(timedelta(hours=8))
NIGHT_START_HOUR = 0
NIGHT_END_HOUR = 5
PUBLISH_HOUR = 17
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
THREE_PLACES = Decimal("0.001")


def local_time(value):
    """页面时间统一显示为值班时区（UTC+08:00）。"""
    if not value:
        return "—"
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(CST).strftime("%Y-%m-%d %H:%M:%S")


def fmt(value):
    """固定三位小数，便于补录前后逐格对照。"""
    return format(Decimal(str(value)).quantize(THREE_PLACES), "f")


def canon_decimal(value):
    """同一读数的不同写法归一，补录重试才能识别为同一条。"""
    number = Decimal(str(value))
    if number == 0:
        return "0"
    return format(number.normalize(), "f")


def parse_at(at):
    try:
        moment = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return moment


def local_night_date(at):
    """读数所属夜次 = 当地 00:00—05:00 窗口的日期；窗口外返回 None。"""
    moment = parse_at(at)
    if moment is None:
        return None
    local = moment.astimezone(CST)
    if NIGHT_START_HOUR <= local.hour < NIGHT_END_HOUR:
        return local.date().isoformat()
    return None


def validate_night_id(night_id):
    if not isinstance(night_id, str) or not DATE_PATTERN.match(night_id):
        reject("NIGHT_ID", "夜次须为 YYYY-MM-DD")
    try:
        datetime.strptime(night_id, "%Y-%m-%d")
    except ValueError:
        reject("NIGHT_ID", "夜次日期无效")
    return night_id


def due_at(night_id):
    """前晚夜次在次日 17:00（UTC+08:00）进入发布时点。"""
    day = datetime.strptime(night_id, "%Y-%m-%d").replace(
        hour=PUBLISH_HOUR, minute=0, second=0, tzinfo=CST
    ) + timedelta(days=1)
    return day.isoformat()


def before_due(night_id, now=None):
    due = datetime.fromisoformat(due_at(night_id))
    now = now or datetime.now(CST)
    return now < due


def night_window(night_id):
    day = datetime.strptime(night_id, "%Y-%m-%d").replace(tzinfo=CST)
    return {"start": day.isoformat(), "end": day.replace(hour=NIGHT_END_HOUR).isoformat(), "timezone": "UTC+08:00"}


def next_night_id(night_id):
    return (datetime.strptime(night_id, "%Y-%m-%d").date() + timedelta(days=1)).isoformat()


def canonical_row(row):
    """补录幂等键只取逻辑读数，忽略文件行号与数值书写格式。"""
    return {
        "record_id": row["record_id"],
        "at": row["at"],
        "meter": row["meter"],
        "from_zone": row["from_zone"],
        "to_zone": row["to_zone"],
        "kind": row["kind"],
        "value_m3": canon_decimal(row["value_m3"]),
        "error_pct": canon_decimal(row["error_pct"]),
    }


def backfill_key(night_id, rows):
    material = json.dumps(
        {
            "night": night_id,
            "rows": sorted(
                (canonical_row(r) for r in rows),
                key=lambda r: json.dumps(r, sort_keys=True),
            ),
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def basis_digest(night_id, run_sha, threshold, rule_version, backfill_keys, carry_keys):
    """采用依据摘要：运行摘要 + 阈值 + 规则版本 + 采纳的补录 + 前班移交。"""
    material = json.dumps(
        {
            "night": night_id,
            "run": run_sha,
            "threshold": str(threshold),
            "rule": rule_version,
            "backfills": sorted(backfill_keys),
            "carryovers": sorted(carry_keys),
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def zone_balances(rows, zones, threshold):
    """按夜次读数计算分区净水量、残差与阈值结论。"""
    items = []
    for item in balances(rows, zones):
        item = dict(item)
        for key in ("inflow", "outflow", "sale", "authorized", "uncertainty", "residual", "lower", "upper"):
            item[key] = fmt(item[key])
        item["net"] = fmt(Decimal(item["inflow"]) - Decimal(item["outflow"]))
        item["status"] = classify(item, threshold)
        items.append(item)
    return items


def totals(results):
    return {
        "net": fmt(sum((Decimal(x["net"]) for x in results), Decimal(0))),
        "residual": fmt(sum((Decimal(x["residual"]) for x in results), Decimal(0))),
        "uncertainty": fmt(sum((Decimal(x["uncertainty"]) for x in results), Decimal(0))),
    }


def apply_rows(base_rows, backfills):
    """迟到的补录按记录号替换读数；同一记录只计一次，原读数进入 superseded 可追回。"""
    index = {}
    order = []
    superseded = []
    for row in base_rows:
        item = copy.deepcopy(row)
        index[item["record_id"]] = item
        order.append(item["record_id"])
    for backfill in sorted(backfills, key=lambda b: (b["received_at"], b["key"])):
        for source in backfill["rows"]:
            item = copy.deepcopy(source)
            record_id = item["record_id"]
            previous = index.get(record_id)
            if previous is not None:
                superseded.append(
                    {
                        "record_id": record_id,
                        "backfill_key": backfill["key"],
                        "received_at": backfill["received_at"],
                        "original": copy.deepcopy(previous),
                        "replacement": copy.deepcopy(item),
                    }
                )
                item["original_line"] = previous.get("original_line", previous.get("line"))
            else:
                item["original_line"] = None
                order.append(record_id)
            item["origin"] = "backfill"
            item["backfill_key"] = backfill["key"]
            index[record_id] = item
    return [index[record_id] for record_id in order], superseded


def backfill_record(rows):
    return [
        {
            "record_id": row["record_id"],
            "meter": row["meter"],
            "from_zone": row["from_zone"],
            "to_zone": row["to_zone"],
            "kind": row["kind"],
            "value_m3": fmt(row["value_m3"]),
            "error_pct": fmt(row["error_pct"]),
            "line": row.get("line"),
            "at": row["at"],
        }
        for row in sorted(rows, key=lambda r: r["record_id"])
    ]


def zone_touch(rows):
    zones = set()
    for row in rows:
        for zone in (row["from_zone"], row["to_zone"]):
            if zone not in {"EXT", "OUT"}:
                zones.add(zone)
    return sorted(zones)


def compare_zones(first, current, preview, backfills):
    """同一晚三层对照：首版正式结论、当前正式结论、补录后净水量与判断。"""
    first_by_zone = {x["zone"]: x for x in (first or [])}
    current_by_zone = {x["zone"]: x for x in (current or [])}
    touched = {}
    for backfill in backfills:
        for zone in zone_touch(backfill["rows"]):
            touched.setdefault(zone, []).extend(row["record_id"] for row in backfill["rows"])
    rows = []
    for after in preview:
        zone = after["zone"]
        first_item = first_by_zone.get(zone)
        current_item = current_by_zone.get(zone)
        net_delta = fmt(Decimal(after["net"]) - Decimal(current_item["net"])) if current_item else None
        residual_delta = (
            fmt(Decimal(after["residual"]) - Decimal(current_item["residual"])) if current_item else None
        )
        rows.append(
            {
                "zone": zone,
                "first_net": first_item["net"] if first_item else None,
                "first_status": first_item["status"] if first_item else "未发布",
                "current_net": current_item["net"] if current_item else None,
                "current_residual": current_item["residual"] if current_item else None,
                "current_status": current_item["status"] if current_item else "未发布",
                "preview_net": after["net"],
                "preview_residual": after["residual"],
                "preview_lower": after["lower"],
                "preview_upper": after["upper"],
                "preview_status": after["status"],
                "net_delta": net_delta,
                "residual_delta": residual_delta,
                "status_changed": bool(current_item and current_item["status"] != after["status"]),
                "records": sorted(set(touched.get(zone, []))),
            }
        )
    return rows


def impact_against(base_results, after_results):
    """一条补录从当前采用依据出发会改动哪些分区。"""
    before = {x["zone"]: x for x in base_results}
    changes = []
    for item in after_results:
        old = before.get(item["zone"])
        if old is None:
            continue
        if old["net"] == item["net"] and old["residual"] == item["residual"] and old["status"] == item["status"]:
            continue
        changes.append(
            {
                "zone": item["zone"],
                "before_net": old["net"],
                "after_net": item["net"],
                "before_residual": old["residual"],
                "after_residual": item["residual"],
                "before_status": old["status"],
                "after_status": item["status"],
                "status_changed": old["status"] != item["status"],
            }
        )
    return changes


def status_of(state):
    """页面状态：有待处置补录优先，其次区分重开、移交、已发布。"""
    if state.get("pending"):
        return "pending", "待处置补录"
    latest = state.get("latest")
    if latest is not None and latest["kind"] == "reopen":
        return "reopened", "已重开"
    if state.get("carry_out"):
        return "carried", "已移交下一班"
    if latest is not None:
        return "published", "已发布"
    return "draft", "未发布"
