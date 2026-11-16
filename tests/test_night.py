import pytest

from app.errors import DomainError
from app.modules.m8_night import service, store
from app.modules.m8_night.rules import backfill_key, local_night_date

NIGHT = "2026-11-06"

AB_500 = """record_id,at,meter,from_zone,to_zone,kind,value,unit,error_pct
06-AB,2026-11-06T02:00:00+08:00,AB,A,B,transfer,500,m3,0.5
"""

SA_410 = """record_id,at,meter,from_zone,to_zone,kind,value,unit,error_pct
06-SA,2026-11-06T02:00:00+08:00,SA,A,OUT,sale,410,m3,0.5
"""


def published():
    service.publish(NIGHT)
    return service.detail(NIGHT)


def test_night_window_is_local():
    assert local_night_date("2026-11-06T02:00:00+08:00") == "2026-11-06"
    assert local_night_date("2026-11-05T18:00:00+00:00") == "2026-11-06"
    assert local_night_date("2026-11-06T06:00:00+08:00") is None


def test_publish_is_idempotent():
    first = service.publish(NIGHT)
    assert first["created"] is True
    again = service.publish(NIGHT)
    assert again["duplicate"] is True
    assert again["detail"]["latest_revision"] == 1
    assert len(again["detail"]["history"]) == 1


def test_publish_rejects_before_due():
    with pytest.raises(DomainError, match="发布"):
        service.publish("2026-11-20")


def test_backfill_retry_does_not_duplicate():
    published()
    first = service.backfill(NIGHT, AB_500, received_at="2026-11-07T17:10:00+08:00")
    retry = service.backfill(NIGHT, AB_500.replace("500", "500.00"), received_at="2026-11-07T18:00:00+08:00")
    assert retry["duplicate"] is True
    assert retry["key"] == first["key"]
    detail = service.detail(NIGHT)
    assert len(detail["backfills"]) == 1
    assert detail["pending_count"] == 1


def test_backfill_changes_net_and_judgement():
    published()
    service.backfill(NIGHT, AB_500, received_at="2026-11-07T17:10:00+08:00")
    detail = service.detail(NIGHT)
    assert detail["status"] == "pending"
    first = next(x for x in detail["comparison"] if x["zone"] == "A")
    assert first["first_net"] == "500.000"
    assert first["current_net"] == "500.000"
    assert first["preview_net"] == "300.000"
    assert first["net_delta"] == "-200.000"
    assert first["status_changed"] is True
    assert first["preview_status"] == "倒挂"
    assert detail["current_totals"]["net"] == "1000.000"
    assert detail["preview_totals"]["net"] == "1000.000"


def test_reopen_creates_one_revision_only():
    published()
    key = service.backfill(NIGHT, AB_500, received_at="2026-11-07T17:10:00+08:00")["key"]
    first = service.reopen(NIGHT, key)
    assert first["created"] is True
    assert first["detail"]["latest_revision"] == 2
    assert first["detail"]["status"] == "reopened"
    again = service.reopen(NIGHT, key)
    assert again["duplicate"] is True
    assert again["detail"]["latest_revision"] == 2
    assert [x["revision"] for x in again["detail"]["history"]] == [1, 2]


def test_reopen_keeps_original_reading():
    published()
    key = service.backfill(NIGHT, AB_500, received_at="2026-11-07T17:10:00+08:00")["key"]
    service.reopen(NIGHT, key)
    versions = service.readings(NIGHT, "06-AB")["versions"]
    values = [x["value_m3"] for x in versions]
    assert "300.000" in values
    assert "500.000" in values
    assert any(x["revision"] == 1 and x["value_m3"] == "300.000" for x in versions)


def test_carry_preserves_publication_and_moves_impact():
    detail = published()
    revision = detail["latest_revision"]
    totals = detail["current_totals"]
    key = service.backfill(NIGHT, SA_410, received_at="2026-11-07T17:10:00+08:00")["key"]
    result = service.carry(NIGHT, key, "不改变结论")
    assert result["detail"]["latest_revision"] == revision
    assert result["detail"]["current_totals"] == totals
    assert result["detail"]["status"] == "carried"
    assert result["detail"]["pending_count"] == 0
    assert result["detail"]["carry_out"][0]["to_night"] == "2026-11-07"
    next_detail = service.detail("2026-11-07")
    assert next_detail["carry_in"][0]["backfill_key"] == key
    assert next_detail["carry_in"][0]["from_night"] == NIGHT
    assert next_detail["carry_in"][0]["impact"][0]["zone"] == "A"
    assert service.carry(NIGHT, key)["duplicate"] is True


def test_cannot_carry_applied_backfill():
    published()
    key = service.backfill(NIGHT, AB_500, received_at="2026-11-07T17:10:00+08:00")["key"]
    service.reopen(NIGHT, key)
    with pytest.raises(DomainError, match="重开"):
        service.carry(NIGHT, key)


def test_publish_blocked_while_pending():
    published()
    service.backfill(NIGHT, AB_500, received_at="2026-11-07T17:10:00+08:00")
    with pytest.raises(DomainError, match="补录"):
        service.publish(NIGHT)


def test_publish_failure_rolls_back(monkeypatch):
    published()
    before = service.detail(NIGHT)
    key = service.backfill(NIGHT, AB_500, received_at="2026-11-07T17:10:00+08:00")["key"]

    def explode(stage):
        raise RuntimeError("写入中断")

    store.set_fault_hook(explode)
    try:
        with pytest.raises(RuntimeError):
            service.reopen(NIGHT, key)
    finally:
        store.set_fault_hook(None)
    after = service.detail(NIGHT)
    assert after["latest_revision"] == before["latest_revision"]
    assert after["current_totals"] == before["current_totals"]
    assert after["pending_count"] == 1
    assert service.readings(NIGHT, "06-AB")["versions"]


def test_first_publish_failure_keeps_backfill(monkeypatch):
    key = service.backfill(NIGHT, AB_500, received_at="2026-11-07T17:10:00+08:00")["key"]

    def explode(stage):
        raise RuntimeError("发布中断")

    store.set_fault_hook(explode)
    try:
        with pytest.raises(RuntimeError):
            service.publish(NIGHT)
    finally:
        store.set_fault_hook(None)
    detail = service.detail(NIGHT)
    assert detail["published"] is False
    assert detail["pending_count"] == 1
    assert detail["backfills"][0]["key"] == key
    assert detail["preview_totals"]["net"] == "1000.000"


def test_backfill_rejects_wrong_night():
    published()
    text = AB_500.replace("06-AB", "05-AB").replace("2026-11-06", "2026-11-05")
    with pytest.raises(DomainError, match="夜次"):
        service.backfill(NIGHT, text)


def test_backfill_rejects_bad_row():
    published()
    text = """record_id,at,meter,from_zone,to_zone,kind,value,unit,error_pct
06-X,2026-11-06T02:00:00+08:00,AB,B,A,transfer,5,m3,0.5
"""
    with pytest.raises(DomainError):
        service.backfill(NIGHT, text)


def test_backfill_retry_with_same_logical_value():
    rows = [
        {"record_id": "06-AB", "at": "2026-11-06T02:00:00+08:00", "meter": "AB", "from_zone": "A",
         "to_zone": "B", "kind": "transfer", "value_m3": "500.000", "error_pct": "0.500"}
    ]
    same = [{**rows[0], "value_m3": "500.0", "error_pct": "0.5", "line": 9}]
    assert backfill_key(NIGHT, rows) == backfill_key(NIGHT, same)
