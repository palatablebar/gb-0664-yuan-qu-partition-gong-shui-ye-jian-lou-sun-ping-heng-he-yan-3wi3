from app.errors import reject
from app.modules.m7_batches.service import run as batch_run
from app.modules.m8_night import store


def latest_run(options=None):
    return batch_run(options or {"sample": "dirty"})


def resolve(night_id=None, run_result=None):
    """解析夜次：默认取最近一个有夜间读数的夜次。"""
    run_result = run_result or latest_run()
    if night_id:
        return night_id, run_result
    night_id = store.latest_night(run_result)
    if not night_id:
        reject("NIGHT_EMPTY", "台账中没有夜间读数")
    return night_id, run_result


def detail(night_id=None, run_result=None):
    night_id, run_result = resolve(night_id, run_result)
    data = store.night_detail(night_id, run_result)
    if not (data["published"] or data["backfills"] or data["record_count"] or data["carry_in"]):
        reject("NIGHT_EMPTY", f"夜次 {night_id} 没有读数或发布记录")
    data["available"] = store.available_nights(run_result)
    data["summaries"] = store.night_summaries()
    return data


def summaries(run_result=None):
    return {"nights": store.night_summaries(), "available": store.available_nights(run_result or latest_run())}


def backfill(night_id, text, received_at=None):
    return store.submit_backfill(night_id, text, received_at)


def publish(night_id, run_result=None):
    night_id, run_result = resolve(night_id, run_result)
    return store.publish(night_id, run_result)


def reopen(night_id, key, run_result=None):
    night_id, run_result = resolve(night_id, run_result)
    return store.reopen(night_id, key, run_result)


def carry(night_id, key, note=None):
    night_id, _ = resolve(night_id)
    return store.carry(night_id, key, note)


def readings(night_id, record_id):
    from app.modules.m8_night.rules import validate_night_id

    validate_night_id(night_id)
    return store.readings(night_id, record_id)


def evidence(night_id, run_result=None):
    return store.evidence_csv(night_id, run_result or latest_run())
