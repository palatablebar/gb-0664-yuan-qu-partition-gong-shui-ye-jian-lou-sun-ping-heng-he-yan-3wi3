from fastapi import APIRouter
from fastapi.responses import Response

from app.errors import DomainError, http_error
from app.modules.m8_night import service

router = APIRouter()


@router.get("/api/night")
def evaluate(night: str | None = None):
    try:
        return service.detail(night)
    except DomainError as exc:
        raise http_error(exc)


@router.get("/api/night/summary")
def summary():
    try:
        return service.summaries()
    except DomainError as exc:
        raise http_error(exc)


@router.get("/api/night/{night}")
def detail(night: str):
    try:
        return service.detail(night)
    except DomainError as exc:
        raise http_error(exc)


@router.post("/api/night/{night}/backfill")
def backfill(night: str, options: dict):
    try:
        return service.backfill(night, options.get("text"), options.get("received_at"))
    except DomainError as exc:
        raise http_error(exc)


@router.post("/api/night/{night}/publish")
def publish(night: str):
    try:
        return service.publish(night)
    except DomainError as exc:
        raise http_error(exc)


@router.post("/api/night/{night}/backfill/{key}/reopen")
def reopen(night: str, key: str):
    try:
        return service.reopen(night, key)
    except DomainError as exc:
        raise http_error(exc)


@router.post("/api/night/{night}/backfill/{key}/carry")
def carry(night: str, key: str, options: dict | None = None):
    try:
        return service.carry(night, key, (options or {}).get("note"))
    except DomainError as exc:
        raise http_error(exc)


@router.get("/api/night/{night}/readings/{record_id}")
def readings(night: str, record_id: str):
    try:
        return service.readings(night, record_id)
    except DomainError as exc:
        raise http_error(exc)


@router.get("/api/night/{night}/evidence.csv")
def evidence(night: str):
    try:
        return Response(
            service.evidence(night),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename=night-{night}-evidence.csv"},
        )
    except DomainError as exc:
        raise http_error(exc)
