import subprocess
import re
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

router = APIRouter()

SACCTMGR = "/opt/slurm/bin/sacctmgr"

AD_GROUPS = [
    "hpc_eeadmins", "hpc_research_beta", "hpc_research_alpha", "hpc_researchers",
    "hpc_faculty", "hpc_course_students", "hpc_matlab_users",
]

QOS_DEFINITIONS = {
    "research":           {"priority": 1000, "max_gpus_job": None,  "max_jobs": 10,   "max_wall": None,        "max_submit": 50},
    "a6000_full":         {"priority": 600,  "max_gpus_job": None,  "max_jobs": None, "max_wall": None,        "max_submit": None},
    "normal":             {"priority": 500,  "max_gpus_job": 2,     "max_jobs": 2,    "max_wall": "1-00:00:00","max_submit": 20},
    "a6000_restricted":   {"priority": 500,  "max_gpus_job": 2,     "max_jobs": None, "max_wall": None,        "max_submit": None},
    "course_batch":       {"priority": 100,  "max_gpus_job": 1,     "max_jobs": 1,    "max_wall": "04:00:00",  "max_submit": 4},
    "course_interactive": {"priority": 150,  "max_gpus_job": 1,     "max_jobs": 2,    "max_wall": "02:00:00",  "max_submit": 4},
}


def _run(cmd: list) -> tuple[int, str, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return 1, "", str(e)


def _fetch_assoc() -> list[dict]:
    rc, out, _ = _run([SACCTMGR, "show", "assoc", "-P",
                       "format=User,Account,DefaultQOS,QOS", "--noheader"])
    if rc != 0:
        return []
    result = []
    for line in out.splitlines():
        p = line.split("|")
        if len(p) < 4 or not p[0].strip():
            continue
        result.append({
            "user":    p[0].strip(),
            "account": p[1].strip(),
            "defqos":  p[2].strip(),
            "qos":     p[3].strip(),
        })
    return result


def _fetch_qos() -> list[dict]:
    rc, out, _ = _run([SACCTMGR, "show", "qos", "-P",
                       "format=Name,Priority,MaxTRESPerJob,MaxTRESPerUser,MaxWall,MaxJobsPerUser,MaxSubmitJobsPerUser",
                       "--noheader"])
    if rc != 0:
        return []
    result = []
    for line in out.splitlines():
        p = line.split("|")
        if len(p) >= 7 and p[0].strip():
            gpus_job = ""
            m = re.search(r"gres/gpu=(\d+)", p[2])
            if m:
                gpus_job = m.group(1)
            gpus_user = ""
            m2 = re.search(r"gres/gpu=(\d+)", p[3])
            if m2:
                gpus_user = m2.group(1)
            result.append({
                "name":          p[0].strip(),
                "priority":      p[1].strip(),
                "max_gpus_job":  gpus_job,
                "max_gpus_user": gpus_user,
                "max_wall":      p[4].strip() or "∞",
                "max_jobs":      p[5].strip() or "∞",
                "max_submit":    p[6].strip() or "∞",
            })
    return result


def _fetch_ad_group(group: str) -> list[str]:
    rc, out, _ = _run(["getent", "group", group])
    if rc != 0 or not out:
        return []
    parts = out.split(":")
    if len(parts) >= 4 and parts[3]:
        return [u.strip() for u in parts[3].split(",") if u.strip()]
    return []


# ── API endpoints ──────────────────────────────────────────────────────────

@router.get("/assoc")
async def get_assoc():
    """All Slurm user associations."""
    assoc = _fetch_assoc()
    # Build accounts map
    accounts: dict[str, list] = {}
    for row in assoc:
        acc = row["account"]
        if acc not in accounts:
            accounts[acc] = []
        accounts[acc].append(row["user"])
    return {"assoc": assoc, "accounts": accounts}


@router.get("/qos")
async def get_qos():
    return {"qos": _fetch_qos()}

class AddUserRequest(BaseModel):
    username: str
    account: str
    defqos: str
    extra_qos: list[str] = []
    prev_account: Optional[str] = ""


class RemoveUserRequest(BaseModel):
    username: str
    account: str


@router.post("/add-user")
async def add_user(req: AddUserRequest):
    if not re.match(r'^[\w\-\.]+$', req.username):
        raise HTTPException(status_code=400, detail="Invalid username")
    all_qos = ",".join(sorted(set([req.defqos] + req.extra_qos)))
    msgs = []

    # Move account if needed
    if req.prev_account and req.prev_account != req.account:
        rc, _, err = _run([SACCTMGR, "-i", "remove", "user", req.username,
                           f"Account={req.prev_account}"])
        msgs.append(f"{'✓' if rc==0 else '✗'} removed from {req.prev_account}")

    # Try modify first, then add
    rc, out, err = _run([SACCTMGR, "-i", "modify", "user", req.username,
                         "where", f"Account={req.account}",
                         "set", f"defaultqos={req.defqos}", f"qos={all_qos}"])
    if rc != 0:
        rc, out, err = _run([SACCTMGR, "-i", "add", "user", req.username,
                             f"Account={req.account}", f"DefaultQOS={req.defqos}",
                             f"QOS={all_qos}"])
    if rc != 0:
        return {"ok": False, "msg": err or out or "sacctmgr error", "hook": []}

    msgs.append(f"✓ {req.username} → {req.account} (defqos={req.defqos})")

    # Post-add hook
    hook = []
    rc1, _, e1 = _run(["sudo", "sss_cache", "-G"])
    hook.append(f"{'✓' if rc1==0 else '✗'} sss_cache -G")
    rc2, _, e2 = _run(["sudo", "systemctl", "restart", "slurmctld"])
    hook.append(f"{'✓' if rc2==0 else '✗'} slurmctld restart")

    return {"ok": True, "msg": " | ".join(msgs), "hook": hook}


@router.post("/remove-user")
async def remove_user(req: RemoveUserRequest):
    if not re.match(r'^[\w\-\.]+$', req.username):
        raise HTTPException(status_code=400, detail="Invalid username")
    rc, out, err = _run([SACCTMGR, "--immediate", "remove", "user",
                         req.username, f"Account={req.account}"])
    if rc != 0:
        return {"ok": False, "msg": err or out}
    return {"ok": True, "msg": f"Removed {req.username} from {req.account}"}


class BulkImportRequest(BaseModel):
    group: str
    account: str
    defqos: str
    extra_qos: list[str] = []


class AddAccountRequest(BaseModel):
    name: str
    description: str = ""
    organization: str = "ECE"


@router.post("/bulk-import")
async def bulk_import(req: BulkImportRequest):
    """Import all members of an AD group into a Slurm account."""
    if not re.match(r'^[\w\-\.]+$', req.group):
        raise HTTPException(status_code=400, detail="Invalid group name")

    # Get members via getent
    members = _fetch_ad_group(req.group)
    if not members:
        return {"ok": False, "summary": f"No members found in group '{req.group}'", "errors": [], "hook": []}

    ok_count = 0
    errors = []
    for user in members:
        all_qos = ",".join(sorted(set([req.defqos] + req.extra_qos)))
        # Try modify, then add
        rc, out, err = _run([SACCTMGR, "-i", "modify", "user", user,
                             "where", f"Account={req.account}",
                             "set", f"defaultqos={req.defqos}", f"qos={all_qos}"])
        if rc != 0:
            rc, out, err = _run([SACCTMGR, "-i", "add", "user", user,
                                 f"Account={req.account}", f"DefaultQOS={req.defqos}",
                                 f"QOS={all_qos}"])
        if rc == 0:
            ok_count += 1
        else:
            errors.append(f"{user}: {err or out}")

    # Post hook
    hook = []
    rc1, _, _ = _run(["sudo", "sss_cache", "-G"])
    hook.append(f"{'✓' if rc1==0 else '✗'} sss_cache -G")
    rc2, _, _ = _run(["sudo", "systemctl", "restart", "slurmctld"])
    hook.append(f"{'✓' if rc2==0 else '✗'} slurmctld restart")

    summary = f"Imported {ok_count}/{len(members)} users from {req.group} → {req.account}"
    if errors:
        summary += f" ({len(errors)} errors)"

    return {"ok": ok_count > 0, "summary": summary, "errors": errors, "hook": hook}


@router.post("/add-account")
async def add_account(req: AddAccountRequest):
    """Create a new Slurm account."""
    if not re.match(r'^[\w\-]+$', req.name):
        raise HTTPException(status_code=400, detail="Invalid account name")
    rc, out, err = _run([SACCTMGR, "-i", "add", "account", req.name,
                         f"Description={req.description}",
                         f"Organization={req.organization}"])
    if rc != 0:
        return {"ok": False, "msg": err or out or "sacctmgr error"}
    return {"ok": True, "msg": f"Created account '{req.name}'"}


from pydantic import BaseModel as _BM
from typing import Optional as _Opt

class QosEditRequest(_BM):
    name: str
    priority: _Opt[str] = ""
    max_wall: _Opt[str] = ""
    max_gpus_job: _Opt[str] = ""
    max_gpus_user: _Opt[str] = ""
    max_jobs: _Opt[str] = ""
    max_submit: _Opt[str] = ""

class QosAssignRequest(_BM):
    qos: str
    user: str
    account: str
    action: str   # add or remove
    set_default: bool = False

@router.post("/qos/edit")
async def edit_qos(req: QosEditRequest):
    import re as _re
    if not _re.match(r'^[\w_\-]+$', req.name):
        return {"ok": False, "msg": "Invalid QoS name"}

    # Build sacctmgr modify command
    parts = [SACCTMGR, "-i", "modify", "qos", req.name, "set"]

    if req.priority:
        parts.append(f"Priority={req.priority}")
    if req.max_wall:
        parts.append(f"MaxWall={req.max_wall}")
    else:
        parts.append("MaxWall=")

    if req.max_gpus_job:
        parts.append(f"MaxTRES=gres/gpu={req.max_gpus_job}")
    else:
        parts.append("MaxTRES=")

    if req.max_gpus_user:
        parts.append(f"MaxTRESPerUser=gres/gpu={req.max_gpus_user}")
    else:
        parts.append("MaxTRESPerUser=")

    if req.max_jobs:
        parts.append(f"MaxJobsPerUser={req.max_jobs}")
    else:
        parts.append("MaxJobsPerUser=")

    if req.max_submit:
        parts.append(f"MaxSubmitJobsPerUser={req.max_submit}")
    else:
        parts.append("MaxSubmitJobsPerUser=")

    try:
        r = subprocess.run(parts, capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return {"ok": False, "msg": r.stderr.strip() or r.stdout.strip()}
        return {"ok": True, "msg": f"QoS '{req.name}' updated"}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


@router.post("/qos/assign")
async def assign_qos(req: QosAssignRequest):
    import re as _re
    for v in [req.qos, req.user, req.account]:
        if not _re.match(r'^[\w_\-]+$', v):
            return {"ok": False, "msg": f"Invalid value: {v}"}

    try:
        # Get current QoS for user
        r = subprocess.run([SACCTMGR, "-P", "-n", "show", "user", req.user,
                            "withassoc", "account=" + req.account,
                            "format=QOS,DefaultQOS"],
                           capture_output=True, text=True, timeout=10)
        current_qos = set()
        current_default = ""
        for line in r.stdout.strip().splitlines():
            parts = line.split("|")
            if len(parts) >= 2:
                current_qos = set(q for q in parts[0].split(",") if q)
                current_default = parts[1].strip()

        if req.action == "add":
            current_qos.add(req.qos)
            new_default = req.qos if req.set_default else current_default
        else:
            current_qos.discard(req.qos)
            new_default = current_default if current_default != req.qos else (next(iter(current_qos)) if current_qos else "normal")

        new_qos_str = ",".join(sorted(current_qos)) or "normal"
        cmd = [SACCTMGR, "-i", "modify", "user", req.user,
               "account=" + req.account, "set",
               f"QOS={new_qos_str}", f"DefaultQOS={new_default}"]
        r2 = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if r2.returncode != 0:
            return {"ok": False, "msg": r2.stderr.strip() or r2.stdout.strip()}
        action_word = "assigned to" if req.action == "add" else "removed from"
        return {"ok": True, "msg": f"QoS '{req.qos}' {action_word} {req.user}@{req.account}"}
    except Exception as e:
        return {"ok": False, "msg": str(e)}
