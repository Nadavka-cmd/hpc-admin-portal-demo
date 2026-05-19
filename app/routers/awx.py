import subprocess
import json
import re
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()

AWX_URL = "http://awx.example.local"
AWX_TOKEN = None  # loaded from config at startup

INVENTORY_ID = 12  # HPC Cluster V2


# Local portal templates:
# id = stable portal/frontend ID
# awx_fallback_id = AWX Job Template ID to launch
# playbook = if set, passed as extra_vars at launch to override the playbook
JOB_TEMPLATES = [
    {
        "id": 1,
        "awx_fallback_id": 25,
        "awx_name": "Install GPU Node @ 01:51:57:5157 PM",
        "playbook": "1_gpu_node_prep.yml",
        "name": "GPU Node — Step 1: Prep",
        "desc": "Prepare OS, packages, networking",
        "icon": "🎮",
        "ask_limit": True,
        "category": "gpu",
        "step": 1,
        "group": "gpu",
    },
    {
        "id": 2,
        "awx_fallback_id": 25,
        "awx_name": "Install GPU Node @ 01:51:57:5157 PM",
        "playbook": "2_gpu_driver_install.yml",
        "name": "GPU Node — Step 2: Driver Install",
        "desc": "Install NVIDIA drivers & CUDA",
        "icon": "🎮",
        "ask_limit": True,
        "category": "gpu",
        "step": 2,
        "group": "gpu",
    },
    {
        "id": 3,
        "awx_fallback_id": 25,
        "awx_name": "Install GPU Node @ 01:51:57:5157 PM",
        "playbook": "3_gpu_node_finalize.yml",
        "name": "GPU Node — Step 3: Finalize",
        "desc": "Configure Slurm, join cluster",
        "icon": "🎮",
        "ask_limit": True,
        "category": "gpu",
        "step": 3,
        "group": "gpu",
    },
    {
        "id": 4,
        "awx_fallback_id": 26,
        "awx_name": "CPU Node — Step 1: Prep",
        "name": "CPU Node — Step 1: Prep",
        "desc": "Prepare a CPU-only node",
        "icon": "⚙️",
        "ask_limit": True,
        "category": "cpu",
        "step": 1,
        "group": "cpu",
    },
    {
        "id": 5,
        "awx_fallback_id": 24,
        "awx_name": "CPU Node — Step 2: Install",
        "name": "CPU Node — Step 2: Install",
        "desc": "Install & join CPU-only node",
        "icon": "⚙️",
        "ask_limit": True,
        "category": "cpu",
        "step": 2,
        "group": "cpu",
    },
    {
        "id": 6,
        "awx_fallback_id": 21,
        "awx_name": "Update /etc/hosts - V2",
        "name": "Update /etc/hosts - V2",
        "desc": "Sync /etc/hosts cluster-wide",
        "icon": "🌐",
        "ask_limit": False,
        "category": "maintenance",
        "step": 0,
        "group": "maint",
    },
    {
        "id": 7,
        "awx_fallback_id": 23,
        "awx_name": "Deploy slurm.conf",
        "name": "Deploy slurm.conf",
        "desc": "Push slurm.conf to all nodes",
        "icon": "📋",
        "ask_limit": False,
        "category": "maintenance",
        "step": 0,
        "group": "maint",
    },
]


def _load_token() -> str:
    global AWX_TOKEN

    if AWX_TOKEN:
        return AWX_TOKEN

    try:
        import yaml
        from pathlib import Path

        cfg_path = Path(__file__).resolve().parent.parent.parent / "config.yaml"
        with open(cfg_path) as f:
            cfg = yaml.safe_load(f)

        AWX_TOKEN = cfg.get("awx", {}).get("token", "")
        url = cfg.get("awx", {}).get("url", AWX_URL)
        globals()["AWX_URL"] = f"http://{url}" if not url.startswith("http") else url
    except Exception:
        pass

    return AWX_TOKEN or ""


def _awx_get(path: str) -> dict:
    token = _load_token()

    try:
        r = subprocess.run(
            [
                "curl",
                "-s",
                "--noproxy",
                "*",
                "-H",
                f"Authorization: Bearer {token}",
                "-H",
                "Content-Type: application/json",
                f"{AWX_URL}{path}",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )

        if r.returncode != 0:
            raise HTTPException(status_code=500, detail=r.stderr or "curl failed")

        return json.loads(r.stdout or "{}")

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _awx_post(path: str, data: dict) -> dict:
    token = _load_token()

    try:
        r = subprocess.run(
            [
                "curl",
                "-s",
                "--noproxy",
                "*",
                "-X",
                "POST",
                "-H",
                f"Authorization: Bearer {token}",
                "-H",
                "Content-Type: application/json",
                "-d",
                json.dumps(data),
                f"{AWX_URL}{path}",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )

        if r.returncode != 0:
            raise HTTPException(status_code=500, detail=r.stderr or "curl failed")

        return json.loads(r.stdout or "{}")

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _awx_patch(path: str, data: dict) -> dict:
    token = _load_token()

    try:
        r = subprocess.run(
            [
                "curl",
                "-s",
                "--noproxy",
                "*",
                "-X",
                "PATCH",
                "-H",
                f"Authorization: Bearer {token}",
                "-H",
                "Content-Type: application/json",
                "-d",
                json.dumps(data),
                f"{AWX_URL}{path}",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )

        if r.returncode != 0:
            raise HTTPException(status_code=500, detail=r.stderr or "curl failed")

        return json.loads(r.stdout or "{}")

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _awx_delete(path: str) -> int:
    token = _load_token()

    try:
        r = subprocess.run(
            [
                "curl",
                "-s",
                "--noproxy",
                "*",
                "-X",
                "DELETE",
                "-H",
                f"Authorization: Bearer {token}",
                f"{AWX_URL}{path}",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return r.returncode

    except Exception:
        return 1


# ── endpoints ──────────────────────────────────────────────────────────────

@router.get("/templates")
async def get_templates():
    templates = []
    for t in JOB_TEMPLATES:
        item = dict(t)
        item.pop("awx_fallback_id", None)
        item.pop("playbook", None)
        templates.append(item)
    return {"templates": templates}


@router.get("/jobs")
async def get_recent_jobs(limit: int = 20):
    data = _awx_get(f"/api/v2/jobs/?page_size={limit}&order_by=-id")
    jobs = []

    for j in data.get("results", []):
        jobs.append(
            {
                "id": j["id"],
                "name": j["name"],
                "status": j["status"],
                "started": j.get("started", ""),
                "finished": j.get("finished", ""),
                "limit": j.get("limit", ""),
                "launched_by": j.get("summary_fields", {})
                .get("launched_by", {})
                .get("username", ""),
            }
        )

    return {"jobs": jobs}


@router.get("/job/{job_id}/output")
async def get_job_output(job_id: int):
    token = _load_token()

    try:
        r = subprocess.run(
            [
                "curl",
                "-s",
                "--noproxy",
                "*",
                "-H",
                f"Authorization: Bearer {token}",
                f"{AWX_URL}/api/v2/jobs/{job_id}/stdout/?format=txt",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return {"output": r.stdout, "job_id": job_id}

    except Exception as e:
        return {"output": str(e), "job_id": job_id}


@router.get("/job/{job_id}")
async def get_job(job_id: int):
    data = _awx_get(f"/api/v2/jobs/{job_id}/")

    return {
        "id": data.get("id"),
        "name": data.get("name"),
        "status": data.get("status"),
        "started": data.get("started"),
        "finished": data.get("finished"),
        "limit": data.get("limit", ""),
        "elapsed": data.get("elapsed", 0),
    }


class LaunchRequest(BaseModel):
    template_id: int
    limit: Optional[str] = ""


@router.post("/launch")
async def launch_job(req: LaunchRequest):
    template = next((t for t in JOB_TEMPLATES if t["id"] == req.template_id), None)

    if not template:
        raise HTTPException(status_code=403, detail="Template not allowed")

    if req.limit and not re.match(r"^[\w\-\.,\*]+$", req.limit):
        raise HTTPException(status_code=400, detail="Invalid limit value")

    awx_template_id = template["awx_fallback_id"]
    playbook = template.get("playbook")

    # If this template requires a specific playbook, patch the AWX template first
    if playbook:
        patch_result = _awx_patch(
            f"/api/v2/job_templates/{awx_template_id}/",
            {"playbook": playbook},
        )
        if "id" not in patch_result:
            detail = patch_result.get("detail") or patch_result.get("msg") or str(patch_result)
            raise HTTPException(status_code=500, detail=f"Failed to set playbook: {detail}")

    payload = {}
    if req.limit:
        payload["limit"] = req.limit

    data = _awx_post(f"/api/v2/job_templates/{awx_template_id}/launch/", payload)

    if "id" not in data:
        detail = data.get("detail") or data.get("msg") or str(data)
        raise HTTPException(status_code=500, detail=detail)

    return {
        "ok": True,
        "job_id": data["id"],
        "status": data.get("status", "pending"),
        "name": data.get("name", ""),
        "awx_template_id": awx_template_id,
        "portal_template_id": req.template_id,
    }


# ── INVENTORY ──────────────────────────────────────────────────────────────

@router.get("/inventory")
async def get_inventory():
    """Get all hosts and groups from the HPC Cluster V2 inventory."""

    groups_data = _awx_get(f"/api/v2/inventories/{INVENTORY_ID}/groups/?page_size=50")
    groups = {}

    for g in groups_data.get("results", []):
        groups[g["id"]] = {"id": g["id"], "name": g["name"], "hosts": []}

    hosts_data = _awx_get(f"/api/v2/inventories/{INVENTORY_ID}/hosts/?page_size=200")
    ungrouped = []

    for h in hosts_data.get("results", []):
        host_info = {
            "id": h["id"],
            "name": h["name"],
            "description": h.get("description", ""),
            "enabled": h.get("enabled", True),
            "variables": h.get("variables", ""),
        }

        hg_data = _awx_get(f"/api/v2/hosts/{h['id']}/groups/?page_size=50")
        host_groups = [g["name"] for g in hg_data.get("results", [])]
        host_info["groups"] = host_groups

        placed = False
        for g in hg_data.get("results", []):
            if g["id"] in groups:
                groups[g["id"]]["hosts"].append(host_info)
                placed = True

        if not placed:
            ungrouped.append(host_info)

    result = list(groups.values())

    if ungrouped:
        result.append({"id": 0, "name": "ungrouped", "hosts": ungrouped})

    return {"groups": result, "inventory_id": INVENTORY_ID}


@router.get("/inventory/groups")
async def get_inventory_groups():
    """Get just the group list for the add-host form."""

    data = _awx_get(f"/api/v2/inventories/{INVENTORY_ID}/groups/?page_size=50")
    groups = [{"id": g["id"], "name": g["name"]} for g in data.get("results", [])]

    return {"groups": groups}


class AddHostRequest(BaseModel):
    hostname: str
    description: Optional[str] = ""
    variables: Optional[str] = ""
    group_ids: list[int] = []


@router.post("/inventory/add-host")
async def add_host(req: AddHostRequest):
    if not re.match(r"^[\w\-\.]+$", req.hostname):
        raise HTTPException(status_code=400, detail="Invalid hostname")

    payload = {
        "name": req.hostname,
        "description": req.description or "",
        "inventory": INVENTORY_ID,
        "enabled": True,
        "variables": req.variables or "",
    }

    data = _awx_post("/api/v2/hosts/", payload)

    if "id" not in data:
        detail = data.get("detail") or data.get("name") or str(data)
        return {"ok": False, "error": str(detail)}

    host_id = data["id"]

    errors = []
    for gid in req.group_ids:
        r = _awx_post(f"/api/v2/groups/{gid}/hosts/", {"id": host_id})
        if "id" not in r and r.get("msg") != "":
            errors.append(f"group {gid}: {r}")

    return {
        "ok": True,
        "host_id": host_id,
        "msg": f"Added '{req.hostname}' to inventory"
        + (f" ({len(req.group_ids)} groups)" if req.group_ids else ""),
        "errors": errors,
    }


class RemoveHostRequest(BaseModel):
    host_id: int
    hostname: str


@router.post("/inventory/remove-host")
async def remove_host(req: RemoveHostRequest):
    if not re.match(r"^[\w\-\.]+$", req.hostname):
        raise HTTPException(status_code=400, detail="Invalid hostname")

    _awx_delete(f"/api/v2/hosts/{req.host_id}/")

    return {"ok": True, "msg": f"Removed '{req.hostname}' from inventory"}
