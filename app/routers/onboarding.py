"""
Onboarding / Offboarding wizard backend.

Group flow:
  1. Check if group exists in AD
     - Found in HPC OU → proceed
     - Found in EE scope but wrong OU → move to HPC OU
     - Found outside EE scope → fail
     - Not found → create in HPC OU
  2. Nest under chosen HPC parent group
  3. Add members
  4. Slurm account + QoS
  5. ZFS quota (optional)

User flow:
  1. Check user exists in AD → hard fail if not
  2. AD group membership
  3. Slurm account + QoS
  4. ZFS quota (optional)
"""

import subprocess
import re
import yaml
from pathlib import Path
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from app.routers.ad_mgmt import (
    _ad_session,
    _find_user_dn,
    _find_group_dn,
    _ldapmodify,
    _ldapadd,
    _ldap_run,
    LDAP_URI,
    LDAP_BASE,
    HPC_OU,
)

router = APIRouter()

SACCTMGR = "/opt/slurm/bin/sacctmgr"
SSH_USER = "demo-admin"
CONFIG_PATH = Path(__file__).resolve().parents[2] / "config.yaml"

EE_OU = "OU=Departments,DC=example,DC=local"

HPC_PARENT_GROUPS = [
    "hpc_researchers",
    "hpc_course_students",
    "hpc_faculty",
    "hpc_eeadmins",
    "hpc_matlab_users",
    "hpc_research_alpha",
    "hpc_research_beta",
]

ZFS_DATASETS = [
    {"label": "home (storage-a)",     "host": "storage-a.example.local", "dataset": "tank2/home",     "idx": 0},
    {"label": "projects (storage-b)", "host": "storage-b.example.local", "dataset": "tank3/projects", "idx": 1},
]

QUOTA_PRESETS = ["none", "50G", "100G", "200G", "500G", "1T", "2T", "5T"]


def _load_config() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _get_group_defaults() -> dict:
    cfg = _load_config()
    return cfg.get("onboarding", {}).get("group_defaults", {})


# ── helpers ───────────────────────────────────────────────────────────────────

def _run(cmd):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return 1, "", str(e)


def _ssh(host, cmd, timeout=30):
    r = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=5",
         "-o", "BatchMode=yes", f"{SSH_USER}@{host}", "bash"],
        input=cmd, capture_output=True, text=True, timeout=timeout,
    )
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def _step(label, ok, msg):
    return {"label": label, "ok": ok, "msg": msg}


def _get_slurm_accounts() -> list:
    rc, out, _ = _run(["sudo", SACCTMGR, "-P", "-n", "show", "account", "format=Account"])
    if rc != 0 or not out:
        return []
    accounts = []
    for l in out.splitlines():
        a = l.split("|")[0].strip()
        if a and a != "root":
            accounts.append(a)
    return sorted(set(accounts))


def _get_slurm_qos() -> list:
    rc, out, _ = _run(["sudo", SACCTMGR, "-P", "-n", "show", "qos", "format=Name"])
    if rc != 0 or not out:
        return []
    qos = []
    for l in out.splitlines():
        q = l.split("|")[0].strip()
        if q and q != "normal":
            qos.append(q)
    return ["normal"] + sorted(set(qos))


def _get_all_hpc_groups() -> list:
    """Get all group CNs under HPC_OU from LDAP."""
    rc, out, _ = _ldap_run([
        "ldapsearch", "-H", LDAP_URI, "-x",
        "-D", _ad_session["dn"], "-w", _ad_session["pw"],
        "-b", HPC_OU, "(objectClass=group)", "cn"
    ])
    if rc != 0 or not out:
        return HPC_PARENT_GROUPS
    groups = []
    for line in out.splitlines():
        if line.lower().startswith("cn:"):
            cn = line.split(":", 1)[1].strip()
            if cn:
                groups.append(cn)
    return sorted(groups) if groups else HPC_PARENT_GROUPS


def _resolve_uid(username):
    r = subprocess.run(["id", "-u", username], capture_output=True, text=True, timeout=5)
    if r.returncode == 0 and r.stdout.strip().isdigit():
        return r.stdout.strip()
    return None


def _slurm_refresh():
    steps = []
    rc1, _, _ = _run(["sudo", "sss_cache", "-G"])
    steps.append(_step("sss_cache -G", rc1 == 0, "cache cleared" if rc1 == 0 else "failed"))
    rc2, _, _ = _run(["sudo", "systemctl", "restart", "slurmctld"])
    steps.append(_step("slurmctld restart", rc2 == 0, "restarted" if rc2 == 0 else "failed"))
    return steps


def _slurm_add_user(username, account, defqos, extra_qos):
    all_qos = ",".join(sorted(set([defqos] + extra_qos)))
    rc, out, err = _run([SACCTMGR, "-i", "modify", "user", username,
                         "where", f"Account={account}",
                         "set", f"defaultqos={defqos}", f"qos={all_qos}"])
    if rc != 0:
        rc, out, err = _run([SACCTMGR, "-i", "add", "user", username,
                             f"Account={account}", f"DefaultQOS={defqos}", f"QOS={all_qos}"])
    if rc != 0:
        return _step("Slurm: add user", False, err or out)
    return _step("Slurm: add user", True, f"{username} → {account} (defqos={defqos})")


def _slurm_remove_user(username, account):
    rc, out, err = _run([SACCTMGR, "--immediate", "remove", "user", username, f"Account={account}"])
    if rc != 0:
        return _step(f"Slurm: remove from {account}", False, err or out)
    return _step(f"Slurm: remove from {account}", True, f"{username} removed from {account}")


def _slurm_create_account(account_name, parent="root"):
    rc, out, err = _run([SACCTMGR, "-i", "add", "account", account_name,
                         f"Parent={parent}", f"Description={account_name}", f"Organization={account_name}"])
    if rc != 0 and "already exists" not in err.lower():
        return _step(f"Slurm: create account {account_name}", False, err or out)
    return _step(f"Slurm: create account {account_name}", True,
                 f"Account '{account_name}' ready" if "already exists" in (err or "").lower()
                 else f"Created account '{account_name}'")


def _slurm_disable_user(username):
    rc, out, _ = _run([SACCTMGR, "-P", "-n", "show", "user", username,
                       "withassoc", "format=Account"])
    accounts = [l.split("|")[0].strip() for l in out.splitlines() if l.strip()]
    if not accounts:
        return _step("Slurm: disable user", True, "No Slurm accounts found")
    results = []
    for acc in accounts:
        rc2, _, _ = _run([SACCTMGR, "-i", "modify", "user", username,
                          "where", f"Account={acc}", "set", "MaxSubmitJobs=0"])
        results.append(f"{'OK' if rc2==0 else 'FAIL'} {acc}")
    return _step("Slurm: disable user", True, f"Blocked: {', '.join(results)}")


def _zfs_set_quota(host, dataset, uid, value):
    cmd = f"sudo zfs set userquota@{uid}={value} {dataset}"
    rc, _, err = _ssh(host, cmd, timeout=15)
    label = f"ZFS quota: {dataset}"
    if rc != 0:
        return _step(label, False, err or "zfs set failed")
    return _step(label, True, f"userquota@{uid}={value}")


def _ad_add_user_to_group(username, group):
    user_dn = _find_user_dn(username)
    if not user_dn:
        return _step(f"AD: add {username} to {group}", False, f"User '{username}' not found in AD")
    group_dn = _find_group_dn(group)
    if not group_dn:
        return _step(f"AD: add {username} to {group}", False, f"Group '{group}' not found")
    ldif = f"dn: {group_dn}\nchangetype: modify\nadd: member\nmember: {user_dn}\n"
    rc, _, err = _ldapmodify(ldif)
    if rc != 0:
        e = err.lower()
        if "already" in e or "type or value exists" in e or "bad_name" in e or "nameerr" in e:
            return _step(f"AD: add {username} to {group}", True, f"{username} already in {group}")
        return _step(f"AD: add {username} to {group}", False, err)
    return _step(f"AD: add {username} to {group}", True, f"{username} → {group}")


def _ad_remove_user_from_group(username, group):
    user_dn = _find_user_dn(username)
    if not user_dn:
        return _step(f"AD: remove {username} from {group}", False, f"User '{username}' not found")
    group_dn = _find_group_dn(group)
    if not group_dn:
        return _step(f"AD: remove {username} from {group}", False, f"Group '{group}' not found")
    ldif = f"dn: {group_dn}\nchangetype: modify\ndelete: member\nmember: {user_dn}\n"
    rc, _, err = _ldapmodify(ldif)
    if rc != 0:
        return _step(f"AD: remove {username} from {group}", False, err)
    return _step(f"AD: remove {username} from {group}", True, f"{username} removed from {group}")


def _find_group_full(group_name):
    rc, out, _ = _ldap_run([
        "ldapsearch", "-H", LDAP_URI, "-x",
        "-D", _ad_session["dn"], "-w", _ad_session["pw"],
        "-b", LDAP_BASE,
        f"(&(objectClass=group)(cn={group_name}))", "dn"
    ])
    if rc != 0 or not out:
        return None
    dn = None
    for line in out.splitlines():
        if line.lower().startswith("dn:"):
            dn = line.split(":", 1)[1].strip()
            break
    if not dn:
        return None
    dn_lower = dn.lower()
    return {
        "dn": dn,
        "in_hpc_ou": HPC_OU.lower() in dn_lower,
        "in_ee_scope": EE_OU.lower() in dn_lower,
    }


def _create_group_in_hpc_ou(group_name, description=""):
    new_dn = f"CN={group_name},{HPC_OU}"
    ldif = (
        f"dn: {new_dn}\nobjectClass: top\nobjectClass: group\n"
        f"cn: {group_name}\nsAMAccountName: {group_name}\n"
        f"description: {description or group_name}\ngroupType: -2147483646\n"
    )
    rc, _, err = _ldapadd(ldif)
    if rc != 0:
        return _step("AD: create group", False, err)
    return _step("AD: create group", True, f"Created CN={group_name} in HPC OU")


def _move_group_to_hpc_ou(old_dn, group_name):
    rc, _, err = _ldap_run([
        "ldapmodrdn", "-H", LDAP_URI, "-x",
        "-D", _ad_session["dn"], "-w", _ad_session["pw"],
        "-r", "-s", HPC_OU, old_dn, f"CN={group_name}"
    ])
    if rc != 0:
        return _step("AD: move group", False, err or "ldapmodrdn failed")
    return _step("AD: move group", True, "Moved to HPC OU")


def _nest_group_under_parent(group_name, parent_group):
    group_dn = _find_group_dn(group_name) or f"CN={group_name},{HPC_OU}"
    parent_dn = _find_group_dn(parent_group)
    if not parent_dn:
        return _step(f"AD: nest under {parent_group}", False, f"Parent '{parent_group}' not found")
    ldif = f"dn: {parent_dn}\nchangetype: modify\nadd: member\nmember: {group_dn}\n"
    rc, _, err = _ldapmodify(ldif)
    if rc != 0 and "already" not in err.lower() and "type or value exists" not in err.lower():
        return _step(f"AD: nest under {parent_group}", False, err)
    return _step(f"AD: nest under {parent_group}", True, f"{group_name} nested under {parent_group}")


# ── Request models ────────────────────────────────────────────────────────────

class CheckGroupRequest(BaseModel):
    group_name: str

class CheckUserRequest(BaseModel):
    username: str

class ValidateMembersRequest(BaseModel):
    usernames: list

class OnboardUserRequest(BaseModel):
    username: str
    ad_groups: list
    slurm_account: str
    slurm_defqos: str
    slurm_extra_qos: list = []
    home_quota: str = "none"
    projects_quota: str = "none"
    set_quota: bool = True
    skel_profile: str = ""
    skip_refresh: bool = False  # skip slurmctld restart for bulk operations

class OffboardUserRequest(BaseModel):
    username: str
    ad_groups: list
    slurm_accounts: list
    disable_only: bool = False

class OnboardGroupRequest(BaseModel):
    group_name: str
    parent_group: str
    members: list
    slurm_account: str
    slurm_defqos: str
    slurm_extra_qos: list = []
    home_quota: str = "none"
    projects_quota: str = "none"
    set_quota: bool = False
    description: str = ""
    skel_profile: str = ""
    create_slurm_account: bool = False
    new_slurm_account_name: str = ""

class OffboardGroupRequest(BaseModel):
    group_name: str
    slurm_account: str

class LookupUserRequest(BaseModel):
    username: str


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/meta")
async def get_meta():
    group_defaults = _get_group_defaults()
    qos_defaults = {}
    quota_defaults = {}
    for g, v in group_defaults.items():
        qos_defaults[g] = {
            "account": v.get("account", ""),
            "defqos":  v.get("defqos", "normal"),
            "extra_qos": v.get("extra_qos", []),
        }
        quota_defaults[g] = {
            "home":     v.get("quota_home", "none"),
            "projects": v.get("quota_projects", "none"),
        }

    return {
        "hpc_parent_groups": _get_all_hpc_groups(),
        "qos_list":       _get_slurm_qos(),
        "qos_defaults":   qos_defaults,
        "quota_defaults": quota_defaults,
        "quota_presets":  QUOTA_PRESETS,
        "zfs_datasets":   ZFS_DATASETS,
        "slurm_accounts": _get_slurm_accounts(),
        "skel_profiles":  list(_get_skel_profiles().keys()),
    }


@router.post("/check-group")
async def check_group(req: CheckGroupRequest):
    if not _ad_session["authenticated"]:
        raise HTTPException(status_code=401, detail="Not authenticated to AD")
    if not re.match(r'^[\w\-\.]+$', req.group_name):
        raise HTTPException(status_code=400, detail="Invalid group name")
    info = _find_group_full(req.group_name)
    if not info:
        return {"exists": False, "action": "create",
                "message": f"'{req.group_name}' not found in AD — will be created in HPC OU."}
    if info["in_hpc_ou"]:
        return {"exists": True, "action": "proceed", "dn": info["dn"],
                "message": "Group found in HPC OU. Ready to proceed."}
    if info["in_ee_scope"]:
        return {"exists": True, "action": "move", "dn": info["dn"],
                "message": f"Group found in EE scope but outside HPC OU — will be moved."}
    return {"exists": True, "action": "fail", "dn": info["dn"],
            "message": f"Group exists outside EE scope ({info['dn']}) — cannot manage."}


@router.post("/check-user")
async def check_user(req: CheckUserRequest):
    if not re.match(r'^[\w\-\.]+$', req.username):
        raise HTTPException(status_code=400, detail="Invalid username")
    dn = _find_user_dn(req.username)
    if not dn:
        return {"exists": False, "message": f"User '{req.username}' not found in AD. Cannot continue."}
    return {"exists": True, "dn": dn, "message": "User found."}


@router.post("/validate-members")
async def validate_members(req: ValidateMembersRequest):
    results = []
    for u in req.usernames:
        if not re.match(r'^[\w\-\.]+$', u):
            results.append({"username": u, "exists": False, "reason": "invalid format"})
            continue
        dn = _find_user_dn(u)
        results.append({"username": u, "exists": bool(dn), "dn": dn or ""})
    return {"results": results}


@router.post("/lookup-user")
async def lookup_user(req: LookupUserRequest):
    if not re.match(r'^[\w\-\.]+$', req.username):
        raise HTTPException(status_code=400, detail="Invalid username")
    rc, out, _ = _run([SACCTMGR, "-P", "-n", "show", "user", req.username,
                       "withassoc", "format=Account,DefaultQOS,QOS"])
    slurm_assoc = []
    for line in out.splitlines():
        p = line.split("|")
        if len(p) >= 3 and p[0].strip():
            slurm_assoc.append({"account": p[0].strip(), "defqos": p[1].strip(), "qos": p[2].strip()})
    ad_memberships = []
    for g in HPC_PARENT_GROUPS:
        rc2, out2, _ = _run(["getent", "group", g])
        if rc2 == 0 and out2:
            parts = out2.split(":")
            if len(parts) >= 4:
                members = [u.strip() for u in parts[3].split(",") if u.strip()]
                if req.username in members:
                    ad_memberships.append(g)
    return {"username": req.username, "slurm_assoc": slurm_assoc, "ad_groups": ad_memberships}


@router.post("/onboard-user")
async def onboard_user(req: OnboardUserRequest):
    if not re.match(r'^[\w\-\.]+$', req.username):
        raise HTTPException(status_code=400, detail="Invalid username")
    steps = []
    if _ad_session["authenticated"]:
        for g in req.ad_groups:
            steps.append(_ad_add_user_to_group(req.username, g))
    else:
        steps.append(_step("AD groups", False, "Not authenticated to AD — skipped"))
    steps.append(_slurm_add_user(req.username, req.slurm_account,
                                  req.slurm_defqos, req.slurm_extra_qos))
    if req.set_quota:
        uid = _resolve_uid(req.username)
        if uid:
            if req.home_quota and req.home_quota != "none":
                steps.append(_zfs_set_quota(ZFS_DATASETS[0]["host"], ZFS_DATASETS[0]["dataset"], uid, req.home_quota))
            if req.projects_quota and req.projects_quota != "none":
                steps.append(_zfs_set_quota(ZFS_DATASETS[1]["host"], ZFS_DATASETS[1]["dataset"], uid, req.projects_quota))
        else:
            steps.append(_step("ZFS quota", False, f"Could not resolve UID for '{req.username}'"))
    if req.skel_profile:
        steps.append(_ensure_homedir(req.username))
        steps.append(_apply_skel(req.username, req.skel_profile))
    # Only restart slurmctld if not part of a bulk operation
    if not req.skip_refresh:
        steps.extend(_slurm_refresh())
    return {"ok": all(s["ok"] for s in steps), "steps": steps}


@router.post("/onboard-user-bulk-refresh")
async def onboard_user_bulk_refresh():
    """Call once after bulk onboarding to restart slurmctld a single time."""
    steps = _slurm_refresh()
    return {"ok": all(s["ok"] for s in steps), "steps": steps}


@router.post("/onboard-group")
async def onboard_group(req: OnboardGroupRequest):
    if not re.match(r'^[\w\-\.]+$', req.group_name):
        raise HTTPException(status_code=400, detail="Invalid group name")
    if not _ad_session["authenticated"]:
        raise HTTPException(status_code=401, detail="Not authenticated to AD")
    steps = []

    info = _find_group_full(req.group_name)
    if not info:
        s = _create_group_in_hpc_ou(req.group_name, req.description)
        steps.append(s)
        if not s["ok"]:
            return {"ok": False, "steps": steps}
    elif not info["in_hpc_ou"]:
        if not info["in_ee_scope"]:
            return {"ok": False, "steps": [_step("AD: locate group", False,
                    f"Group exists outside EE scope — cannot manage: {info['dn']}")]}
        s = _move_group_to_hpc_ou(info["dn"], req.group_name)
        steps.append(s)
        if not s["ok"]:
            return {"ok": False, "steps": steps}
    else:
        steps.append(_step("AD: locate group", True, "Group already in HPC OU"))

    steps.append(_nest_group_under_parent(req.group_name, req.parent_group))

    valid_members = []
    for username in req.members:
        if not re.match(r'^[\w\-\.]+$', username):
            steps.append(_step(f"AD: add {username}", False, "Invalid username format"))
            continue
        s = _ad_add_user_to_group(username, req.group_name)
        steps.append(s)
        if s["ok"]:
            valid_members.append(username)

    if not valid_members:
        steps.append(_step("Members", False, "No valid members — stopping"))
        return {"ok": False, "steps": steps}

    # Create new Slurm account if requested
    effective_account = req.slurm_account
    if req.create_slurm_account and req.new_slurm_account_name:
        effective_account = req.new_slurm_account_name
        s = _slurm_create_account(effective_account)
        steps.append(s)
        if not s["ok"]:
            return {"ok": False, "steps": steps}

    all_qos = ",".join(sorted(set([req.slurm_defqos] + req.slurm_extra_qos)))
    ok_count, errors = 0, []
    for user in valid_members:
        rc, out, err = _run([SACCTMGR, "-i", "modify", "user", user,
                             "where", f"Account={effective_account}",
                             "set", f"defaultqos={req.slurm_defqos}", f"qos={all_qos}"])
        if rc != 0:
            rc, out, err = _run([SACCTMGR, "-i", "add", "user", user,
                                 f"Account={effective_account}",
                                 f"DefaultQOS={req.slurm_defqos}", f"QOS={all_qos}"])
        if rc == 0:
            ok_count += 1
        else:
            errors.append(f"{user}: {err or out}")
    steps.append(_step(f"Slurm: {len(valid_members)} users → {effective_account}", ok_count > 0,
        f"{ok_count}/{len(valid_members)} added" + (f"; errors: {'; '.join(errors)}" if errors else "")))

    if req.set_quota:
        quota_ok = 0
        for user in valid_members:
            uid = _resolve_uid(user)
            if not uid:
                continue
            if req.home_quota and req.home_quota != "none":
                r = _zfs_set_quota(ZFS_DATASETS[0]["host"], ZFS_DATASETS[0]["dataset"], uid, req.home_quota)
                if r["ok"]:
                    quota_ok += 1
            if req.projects_quota and req.projects_quota != "none":
                _zfs_set_quota(ZFS_DATASETS[1]["host"], ZFS_DATASETS[1]["dataset"], uid, req.projects_quota)
        steps.append(_step("ZFS quota", True, f"Set for {quota_ok} users"))

    if req.skel_profile:
        for user in valid_members:
            steps.append(_ensure_homedir(user))
            steps.append(_apply_skel(user, req.skel_profile))

    steps.extend(_slurm_refresh())
    return {"ok": all(s["ok"] for s in steps), "steps": steps, "member_count": len(valid_members)}


@router.post("/offboard-user")
async def offboard_user(req: OffboardUserRequest):
    if not re.match(r'^[\w\-\.]+$', req.username):
        raise HTTPException(status_code=400, detail="Invalid username")
    steps = []
    if _ad_session["authenticated"]:
        for g in req.ad_groups:
            steps.append(_ad_remove_user_from_group(req.username, g))
    else:
        steps.append(_step("AD groups", False, "Not authenticated to AD — skipped"))
    if req.disable_only:
        steps.append(_slurm_disable_user(req.username))
    else:
        for acc in req.slurm_accounts:
            steps.append(_slurm_remove_user(req.username, acc))
    steps.extend(_slurm_refresh())
    return {"ok": all(s["ok"] for s in steps), "steps": steps}


@router.post("/offboard-group")
async def offboard_group(req: OffboardGroupRequest):
    if not re.match(r'^[\w\-\.]+$', req.group_name):
        raise HTTPException(status_code=400, detail="Invalid group name")
    rc, out, _ = _run(["getent", "group", req.group_name])
    members = []
    if rc == 0 and out:
        parts = out.split(":")
        if len(parts) >= 4 and parts[3]:
            members = [u.strip() for u in parts[3].split(",") if u.strip()]
    if not members:
        return {"ok": False, "steps": [_step("Resolve members", False,
                f"No members found in '{req.group_name}'")]}
    steps = [_step("Resolve members", True, f"{len(members)} members found")]
    ok_count, errors = 0, []
    for user in members:
        rc2, _, err2 = _run([SACCTMGR, "--immediate", "remove", "user", user,
                              f"Account={req.slurm_account}"])
        if rc2 == 0:
            ok_count += 1
        else:
            errors.append(f"{user}: {err2}")
    steps.append(_step(f"Slurm: remove from {req.slurm_account}", True,
        f"{ok_count}/{len(members)} removed" + (f"; errors: {'; '.join(errors)}" if errors else "")))
    steps.extend(_slurm_refresh())
    return {"ok": all(s["ok"] for s in steps), "steps": steps}


# ── Skel ──────────────────────────────────────────────────────────────────────

def _get_skel_profiles() -> dict:
    cfg = _load_config()
    return cfg.get("onboarding", {}).get("skel_profiles", {})

def _get_home_base() -> str:
    cfg = _load_config()
    return cfg.get("onboarding", {}).get("home_base", "/demo/home")

def _ensure_homedir(username: str) -> dict:
    home_base = _get_home_base()
    home_dir = f"{home_base}/{username}"

    rc, _, _ = _run(["test", "-d", home_dir])
    if rc == 0:
        return _step("Homedir", True, f"{home_dir} already exists")

    rc, _, err = _run(["sudo", "/usr/sbin/oddjob_request", "-s",
                       "com.redhat.oddjob_mkhomedir", "-o", "/",
                       "mkhomedirfor", username])
    if rc == 0:
        return _step("Homedir", True, f"Created {home_dir}")

    rc2, _, err2 = _run(["sudo", "/usr/sbin/mkhomedir_helper", username])
    if rc2 == 0:
        return _step("Homedir", True, f"Created {home_dir} (via mkhomedir_helper)")

    return _step("Homedir", False, f"Could not create {home_dir}: {err or err2}")


def _apply_skel(username: str, profile: str) -> dict:
    """Apply a skel profile to a user's home directory.
    Paths are fully defined in config.yaml — no substitution needed.
    """
    profiles = _get_skel_profiles()
    if profile not in profiles:
        return _step(f"Skel: {profile}", False, f"Profile '{profile}' not found in config")

    home_base = _get_home_base()
    home_dir = f"{home_base}/{username}"

    rc, _, _ = _run(["test", "-d", home_dir])
    if rc != 0:
        return _step(f"Skel: {profile}", False, f"Home directory {home_dir} does not exist")

    symlinks = profiles[profile].get("symlinks", [])
    results = []
    all_ok = True

    for link in symlinks:
        src = link["src"].replace("{username}", username)
        dst_name = link["dst"].replace("{username}", username)
        dst_path = f"{home_dir}/{dst_name}"

        rc, _, _ = _run(["test", "-e", dst_path])
        if rc == 0:
            results.append(f"✓ {dst_name} (already exists)")
            continue

        rc, _, _ = _run(["test", "-e", src])
        if rc != 0:
            results.append(f"✗ {dst_name} (source {src} not found)")
            all_ok = False
            continue

        rc, _, err = _run(["sudo", "-u", username, "ln", "-s", src, dst_path])
        if rc != 0:
            rc2, _, err2 = _run(["ln", "-s", src, dst_path])
            if rc2 != 0:
                results.append(f"✗ {dst_name}: {err2 or err}")
                all_ok = False
                continue
            _run(["chown", "-h", f"{username}:", dst_path])

        results.append(f"✓ {dst_name} → {src}")

    return _step(f"Skel: {profile}", all_ok, "; ".join(results))


class ApplySkelRequest(BaseModel):
    username: str
    profile: str


@router.get("/skel-profiles")
async def get_skel_profiles():
    return {"profiles": list(_get_skel_profiles().keys())}


@router.post("/apply-skel")
async def apply_skel(req: ApplySkelRequest):
    if not re.match(r'^[\w\-\.]+$', req.username):
        raise HTTPException(status_code=400, detail="Invalid username")
    result = _apply_skel(req.username, req.profile)
    return {"ok": result["ok"], "steps": [result]}
