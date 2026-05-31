import subprocess
import re
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()

def _load_zfs_cfg():
    import yaml
    from pathlib import Path
    cfg = yaml.safe_load(open(Path(__file__).resolve().parent.parent.parent / "config.yaml"))
    return (cfg.get("ssh",{}).get("user","profadmin"),
            cfg.get("zfs",{}).get("datasets",[]))

SSH_USER, ZFS_DATASETS = _load_zfs_cfg()

QUOTA_PRESETS = ["none", "50G", "100G", "200G", "500G", "1T", "2T", "5T"]


def _ssh(host: str, cmd: str, timeout: int = 30) -> tuple[int, str, str]:
    r = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=5",
         "-o", "BatchMode=yes", f"{SSH_USER}@{host}", "bash"],
        input=cmd, capture_output=True, text=True, timeout=timeout,
    )
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def _resolve_uid(uid: str) -> str:
    """Try to resolve a numeric UID to a username via getent."""
    try:
        uid_int = int(uid)
        if uid_int < 1000:
            return uid
        r = subprocess.run(
            ["getent", "passwd", uid],
            capture_output=True, text=True, timeout=5
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip().split(":")[0]
    except Exception:
        pass
    return uid


def _fetch_userspace(host: str, dataset: str) -> list[dict]:
    cmd = f"sudo zfs userspace -H -o type,name,used,quota {dataset}"
    rc, out, err = _ssh(host, cmd, timeout=30)
    if rc != 0 or not out:
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        typ, uid, used, quota = parts
        if typ not in ("POSIX User", "SMB User"):
            continue
        username = _resolve_uid(uid)
        rows.append({
            "uid":      uid,
            "username": username,
            "used":     used,
            "quota":    quota,
        })
    rows.sort(key=lambda r: r["username"].lower())
    return rows


# ── endpoints ──────────────────────────────────────────────────────────────

@router.get("/datasets")
async def get_datasets():
    return {"datasets": ZFS_DATASETS, "presets": QUOTA_PRESETS}


@router.get("/userspace/{dataset_idx}")
async def get_userspace(dataset_idx: int):
    if dataset_idx < 0 or dataset_idx >= len(ZFS_DATASETS):
        raise HTTPException(status_code=404, detail="Dataset not found")
    ds = ZFS_DATASETS[dataset_idx]
    rows = _fetch_userspace(ds["host"], ds["dataset"])
    return {
        "dataset": ds,
        "rows": rows,
        "total_users": len(rows),
    }


class SetQuotaRequest(BaseModel):
    dataset_idx: int
    uid: str          # always use UID — TrueNAS doesn't resolve AD usernames
    username: str     # for display only
    value: str        # e.g. "500G", "1T", "none"


@router.post("/set-quota")
async def set_quota(req: SetQuotaRequest):
    if req.dataset_idx < 0 or req.dataset_idx >= len(ZFS_DATASETS):
        raise HTTPException(status_code=404, detail="Dataset not found")
    # Validate value
    if req.value != "none" and not re.match(r'^\d+(\.\d+)?[KMGTP]?$', req.value, re.IGNORECASE):
        raise HTTPException(status_code=400, detail="Invalid quota value")
    # Validate UID
    if not re.match(r'^[\w\-\.@]+$', req.uid):
        raise HTTPException(status_code=400, detail="Invalid UID")

    ds = ZFS_DATASETS[req.dataset_idx]
    cmd = f"sudo zfs set userquota@{req.uid}={req.value} {ds['dataset']}"
    rc, _, err = _ssh(ds["host"], cmd, timeout=15)
    if rc != 0:
        return {"ok": False, "error": err or "zfs set failed"}
    return {"ok": True, "msg": f"Quota set to {req.value} for {req.username} (uid {req.uid}) on {ds['dataset']}"}
