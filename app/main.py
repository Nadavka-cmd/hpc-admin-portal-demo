from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import JSONResponse, RedirectResponse
from pathlib import Path

from app.routers import slurm, storage, config_sync, accounts, ad_mgmt, zfs_quota, awx, onboarding, mail

BASE_DIR = Path(__file__).resolve().parent.parent
app = FastAPI(title="HPC Portal", version="0.1.0", root_path="/hpc-portal")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app.include_router(slurm.router,       prefix="/api/slurm",    tags=["slurm"])
app.include_router(storage.router,     prefix="/api/storage",  tags=["storage"])
app.include_router(config_sync.router, prefix="/api/sync",     tags=["sync"])
app.include_router(accounts.router,    prefix="/api/accounts", tags=["accounts"])
app.include_router(ad_mgmt.router,     prefix="/api/ad",       tags=["ad"])
app.include_router(zfs_quota.router,   prefix="/api/quota",    tags=["quota"])
app.include_router(awx.router,         prefix="/api/awx",      tags=["awx"])
app.include_router(onboarding.router,  prefix="/api/onboarding", tags=["onboarding"])
app.include_router(mail.router,        prefix="/api/mail",     tags=["mail"])

@app.get("/health")
async def health():
    return JSONResponse({"status": "ok"})

@app.get("/")
async def root(request: Request):
    return templates.TemplateResponse("slurm_admin.html", {"request": request, "root_path": request.scope.get("root_path", "")})

@app.get("/slurm")
async def slurm_admin(request: Request):
    return templates.TemplateResponse("slurm_admin.html", {"request": request, "root_path": request.scope.get("root_path", "")})

@app.get("/jobs")
async def jobs_redirect():
    return RedirectResponse(url="/slurm#jobs")

@app.get("/storage")
async def storage_page(request: Request):
    return templates.TemplateResponse("storage.html", {"request": request, "root_path": request.scope.get("root_path", "")})

@app.get("/actions")
async def actions_page(request: Request):
    return templates.TemplateResponse("actions.html", {"request": request, "root_path": request.scope.get("root_path", "")})
