from fastapi import APIRouter,Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from app.config import TITLE,MODULES,RULE_VERSION
from app.modules.m7_batches.service import run
from app.store import fetch
from app.modules.m8_night.service import detail as night_detail
from app.errors import DomainError
router=APIRouter();templates=Jinja2Templates(directory="app/templates")
@router.get("/")
def home():return RedirectResponse("/calculate")
def page(request:Request,slug:str,run_id:str|None=None):
    if slug=="night":
        requested=request.query_params.get("night") or None
        try:
            data=night_detail(requested)
        except DomainError:
            if requested is None:raise
            return RedirectResponse("/night",status_code=303)
    else:
        data=fetch(run_id) if run_id else run({"sample":"dirty"})
    return templates.TemplateResponse(request=request,name=slug+".html",context={"data":data,"title":TITLE,"modules":MODULES,"slug":slug,"page_title":dict(MODULES)[slug],"version":RULE_VERSION})
for slug,label in MODULES:
    def view(request:Request,run_id:str|None=None,_slug=slug):return page(request,_slug,run_id)
    router.add_api_route("/"+slug,view,methods=["GET"],include_in_schema=False)
