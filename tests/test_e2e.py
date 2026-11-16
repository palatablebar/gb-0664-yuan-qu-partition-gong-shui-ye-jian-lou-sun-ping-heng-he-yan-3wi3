from fastapi.testclient import TestClient
from app.main import app
from app.config import MODULES
def test_all_pages_and_api():
    with TestClient(app) as c:
        for slug,label in MODULES:
            assert c.get("/"+slug).status_code==200
            assert c.get("/api/"+slug).status_code==200
def test_run_and_stale_api():
    with TestClient(app) as c:
        a=c.post("/api/runs",json={"sample":"dirty"}).json();rid=a["manifest"]["run_id"]
        assert c.post(f"/api/runs/{rid}/review",json={"revision":1,"decision":"通过"}).status_code==200
        assert c.post(f"/api/runs/{rid}/review",json={"revision":1,"decision":"通过"}).status_code==409
        assert c.get(f"/api/runs/{rid}/evidence.csv").status_code==200
def test_bad_input_api():
    with TestClient(app) as c:assert c.post("/api/runs",json={"text":"wrong"}).status_code==422

def test_night_page_and_api():
    text="record_id,at,meter,from_zone,to_zone,kind,value,unit,error_pct\n06-AB,2026-11-06T02:00:00+08:00,AB,A,B,transfer,500,m3,0.5\n"
    with TestClient(app) as c:
        assert c.get("/night").status_code==200
        assert c.post("/api/night/2026-11-06/publish").json()["created"] is True
        key=c.post("/api/night/2026-11-06/backfill",json={"text":text}).json()["key"]
        page=c.get("/night?night=2026-11-06")
        assert page.status_code==200
        assert "重开当班" in page.text and "保留原结论并移交下一班" in page.text
        detail=c.get("/api/night/2026-11-06").json()
        assert detail["pending_count"]==1
        assert c.get("/api/night/2026-11-06/evidence.csv").status_code==200
        assert c.get("/api/night/2026-11-06/readings/06-AB").json()["versions"]
        assert c.post(f"/api/night/2026-11-06/backfill/{key}/reopen").json()["detail"]["latest_revision"]==2
        assert c.post(f"/api/night/2026-11-06/backfill/{key}/reopen").json()["duplicate"] is True
        assert c.get("/night?night=2030-01-01",follow_redirects=False).status_code==303
