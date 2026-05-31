from typing import Optional
import subprocess
import re
import os
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()

def _load_ldap_cfg():
    import yaml
    from pathlib import Path
    cfg = yaml.safe_load(open(Path(__file__).resolve().parent.parent.parent / "config.yaml"))
    l = cfg.get("ldap", {})
    return l.get("uri","ldap://localhost"), l.get("base",""), l.get("hpc_ou",""), l.get("domain","")

LDAP_URI, LDAP_BASE, HPC_OU, AD_DOMAIN = _load_ldap_cfg()

HPC_PARENT_GROUPS = [
    "hpc_eeadmins",
    "hpc_researchers",
    "hpc_faculty",
    "hpc_course_students",
    "hpc_adrian",
    "hpc_permuter",
    "hpc_matlab_users",
]

_ad_session: dict = {"dn": "", "pw": "", "authenticated": False}


def _ldap_run(args: list, input_data: str = "") -> tuple[int, str, str]:
    try:
        r = subprocess.run(
            args, input=input_data,
            capture_output=True, text=True, timeout=15
        )
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return 1, "", str(e)


def _bind_dn() -> str:
    return _ad_session["dn"]


def _bind_pw() -> str:
    return _ad_session["pw"]


def _ldapsearch(filter_str: str, attrs: list, base: str = HPC_OU) -> tuple[int, str, str]:
    cmd = [
        "ldapsearch", "-H", LDAP_URI, "-x",
        "-D", _bind_dn(), "-w", _bind_pw(),
        "-b", base, filter_str,
    ] + attrs
    return _ldap_run(cmd)


def _find_group_dn(group_name: str) -> Optional[str]:
    rc, out, err = _ldapsearch(f"(cn={group_name})", ["dn"], base=LDAP_BASE)
    if rc != 0:
        return None
    lines = []
    for line in out.splitlines():
        if line.startswith(" ") and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    for line in lines:
        if line.lower().startswith("dn:"):
            return line.split(":", 1)[1].strip()
    return None


def _find_user_dn(username: str) -> Optional[str]:
    rc, out, err = _ldapsearch(
        f"(sAMAccountName={username})", ["dn"],
        base=LDAP_BASE
    )
    if rc != 0:
        return None
    lines = []
    for line in out.splitlines():
        if line.startswith(" ") and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    for line in lines:
        if line.lower().startswith("dn:"):
            return line.split(":", 1)[1].strip()
    return None


def _get_group_members(group_name: str) -> list:
    rc, out, err = _ldapsearch(f"(cn={group_name})", ["member"], base=LDAP_BASE)
    members = []
    if rc != 0:
        return members
    lines = []
    for line in out.splitlines():
        if line.startswith(" ") and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    for line in lines:
        if line.lower().startswith("member:"):
            dn = line.split(":", 1)[1].strip()
            m = re.match(r"CN=([^,]+)", dn, re.IGNORECASE)
            if m:
                members.append(m.group(1))
    return members


def _get_group_members_typed(group_name: str) -> dict:
    rc, out, err = _ldapsearch(f"(cn={group_name})", ["member"], base=LDAP_BASE)
    if rc != 0:
        return {"users": [], "groups": []}

    lines = []
    for line in out.splitlines():
        if line.startswith(" ") and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)

    member_dns = []
    member_cns = []
    for line in lines:
        if line.lower().startswith("member:"):
            dn = line.split(":", 1)[1].strip()
            cn_match = re.match(r"CN=([^,]+)", dn, re.IGNORECASE)
            if cn_match:
                member_dns.append(dn)
                member_cns.append(cn_match.group(1))

    if not member_dns:
        return {"users": [], "groups": []}

    if len(member_cns) == 1:
        group_filter = f"(&(objectClass=group)(cn={member_cns[0]}))"
    else:
        cn_filters = "".join(f"(cn={cn})" for cn in member_cns)
        group_filter = f"(&(objectClass=group)(|{cn_filters}))"

    rc2, out2, _ = _ldap_run([
        "ldapsearch", "-H", LDAP_URI, "-x",
        "-D", _bind_dn(), "-w", _bind_pw(),
        "-b", LDAP_BASE, group_filter, "cn"
    ])

    group_cns = set()
    for line in out2.splitlines():
        if line.lower().startswith("cn:"):
            group_cns.add(line.split(":", 1)[1].strip())

    users = []
    child_groups = []
    for i, cn in enumerate(member_cns):
        if cn in group_cns:
            child_groups.append({"name": cn, "dn": member_dns[i]})
        else:
            users.append(cn)

    return {"users": users, "groups": child_groups}


def _ldapmodify(ldif: str) -> tuple[int, str, str]:
    cmd = [
        "ldapmodify", "-H", LDAP_URI, "-x",
        "-D", _bind_dn(), "-w", _bind_pw(),
    ]
    return _ldap_run(cmd, input_data=ldif)


def _ldapadd(ldif: str) -> tuple[int, str, str]:
    cmd = [
        "ldapadd", "-H", LDAP_URI, "-x",
        "-D", _bind_dn(), "-w", _bind_pw(),
    ]
    return _ldap_run(cmd, input_data=ldif)


class ADAuthRequest(BaseModel):
    username: str
    password: str


class GroupMemberRequest(BaseModel):
    group: str
    username: str


class CreateGroupRequest(BaseModel):
    name: str
    parent_group: str
    description: str = ""


class AddChildGroupRequest(BaseModel):
    parent_group: str
    child_group: str


@router.post("/auth")
async def ad_auth(req: ADAuthRequest):
    dn = req.username if "@" in req.username else f"{req.username}@{AD_DOMAIN}"
    rc, out, err = _ldap_run([
        "ldapsearch", "-H", LDAP_URI, "-x",
        "-D", dn, "-w", req.password,
        "-b", HPC_OU, "-s", "base", "(objectClass=*)", "cn"
    ])
    if rc != 0:
        _ad_session["authenticated"] = False
        return {"ok": False, "error": err.strip() or "Authentication failed"}
    _ad_session["dn"] = dn
    _ad_session["pw"] = req.password
    _ad_session["authenticated"] = True
    return {"ok": True, "dn": dn}


@router.get("/session")
async def get_session():
    return {"authenticated": _ad_session["authenticated"], "dn": _ad_session.get("dn", "")}


@router.post("/logout")
async def logout():
    _ad_session["dn"] = ""
    _ad_session["pw"] = ""
    _ad_session["authenticated"] = False
    return {"ok": True}


@router.get("/groups")
async def get_groups():
    if not _ad_session["authenticated"]:
        raise HTTPException(status_code=401, detail="Not authenticated to AD")

    # First pass: resolve all groups
    resolved = {}
    for group in HPC_PARENT_GROUPS:
        typed = _get_group_members_typed(group)
        users = typed["users"]
        child_group_dns = typed["groups"]

        children_detail = []
        for cg in child_group_dns:
            cg_typed = _get_group_members_typed(cg["name"])
            children_detail.append({
                "name": cg["name"],
                "members": cg_typed["users"],
                "child_groups": cg_typed["groups"],
                "count": len(cg_typed["users"]) + len(cg_typed["groups"]),
            })

        resolved[group] = {
            "name": group,
            "direct_users": users,
            "child_groups": children_detail,
            "total_members": len(users) + len(child_group_dns),
        }

    # Collect all group names that appear as children of any other group
    child_names = set()
    for entry in resolved.values():
        for cg in entry["child_groups"]:
            child_names.add(cg["name"])

    # Only emit top-level entries for groups not nested under another
    result = [v for k, v in resolved.items() if k not in child_names]

    return {"groups": result}


@router.get("/group/{group_name}/members")
async def get_group_members(group_name: str):
    if not _ad_session["authenticated"]:
        raise HTTPException(status_code=401, detail="Not authenticated to AD")
    if not re.match(r'^[\w\-\.]+$', group_name):
        raise HTTPException(status_code=400, detail="Invalid group name")
    members = _get_group_members(group_name)
    return {"group": group_name, "members": members}


@router.post("/group/add-member")
async def add_member(req: GroupMemberRequest):
    if not _ad_session["authenticated"]:
        raise HTTPException(status_code=401, detail="Not authenticated to AD")
    if not re.match(r'^[\w\-\.]+$', req.group) or not re.match(r'^[\w\-\.@]+$', req.username):
        raise HTTPException(status_code=400, detail="Invalid input")

    user_dn = _find_user_dn(req.username)
    if not user_dn:
        return {"ok": False, "error": f"User '{req.username}' not found in AD"}

    group_dn = _find_group_dn(req.group)
    if not group_dn:
        return {"ok": False, "error": f"Group '{req.group}' not found in AD"}

    ldif = f"dn: {group_dn}\nchangetype: modify\nadd: member\nmember: {user_dn}\n"
    rc, out, err = _ldapmodify(ldif)
    if rc != 0:
        if "already exists" in err.lower() or "already a member" in err.lower() or "type or value exists" in err.lower():
            return {"ok": True, "msg": f"{req.username} is already a member of {req.group}"}
        return {"ok": False, "error": err or out}
    return {"ok": True, "msg": f"Added {req.username} to {req.group}"}


@router.post("/group/remove-member")
async def remove_member(req: GroupMemberRequest):
    if not _ad_session["authenticated"]:
        raise HTTPException(status_code=401, detail="Not authenticated to AD")
    if not re.match(r'^[\w\-\.]+$', req.group) or not re.match(r'^[\w\-\.@]+$', req.username):
        raise HTTPException(status_code=400, detail="Invalid input")

    user_dn = _find_user_dn(req.username)
    if not user_dn:
        return {"ok": False, "error": f"User '{req.username}' not found in AD"}

    group_dn = _find_group_dn(req.group)
    if not group_dn:
        return {"ok": False, "error": f"Group '{req.group}' not found in AD"}

    ldif = f"dn: {group_dn}\nchangetype: modify\ndelete: member\nmember: {user_dn}\n"
    rc, out, err = _ldapmodify(ldif)
    if rc != 0:
        return {"ok": False, "error": err or out}
    return {"ok": True, "msg": f"Removed {req.username} from {req.group}"}


@router.post("/group/create")
async def create_group(req: CreateGroupRequest):
    if not _ad_session["authenticated"]:
        raise HTTPException(status_code=401, detail="Not authenticated to AD")
    if not re.match(r'^[\w\-\.]+$', req.name):
        raise HTTPException(status_code=400, detail="Invalid group name")

    new_dn = f"CN={req.name},{HPC_OU}"
    ldif = (
        f"dn: {new_dn}\n"
        f"objectClass: top\n"
        f"objectClass: group\n"
        f"cn: {req.name}\n"
        f"sAMAccountName: {req.name}\n"
        f"description: {req.description or req.name}\n"
        f"groupType: -2147483646\n"
    )
    rc, out, err = _ldapadd(ldif)
    if rc != 0:
        return {"ok": False, "error": err or out}

    parent_dn = _find_group_dn(req.parent_group)
    if not parent_dn:
        return {"ok": False, "error": f"Group '{req.name}' created but parent '{req.parent_group}' not found for nesting"}

    ldif2 = f"dn: {parent_dn}\nchangetype: modify\nadd: member\nmember: {new_dn}\n"
    rc2, out2, err2 = _ldapmodify(ldif2)
    if rc2 != 0:
        return {"ok": False, "error": f"Group '{req.name}' created but nesting failed: {err2 or out2}"}

    return {"ok": True, "msg": f"Created '{req.name}' and nested under '{req.parent_group}'"}


@router.post("/group/add-child")
async def add_child_group(req: AddChildGroupRequest):
    if not _ad_session["authenticated"]:
        raise HTTPException(status_code=401, detail="Not authenticated to AD")

    child_dn = _find_group_dn(req.child_group)
    if not child_dn:
        return {"ok": False, "error": f"Group '{req.child_group}' not found"}

    parent_dn = _find_group_dn(req.parent_group)
    if not parent_dn:
        return {"ok": False, "error": f"Parent group '{req.parent_group}' not found"}

    ldif = f"dn: {parent_dn}\nchangetype: modify\nadd: member\nmember: {child_dn}\n"
    rc, out, err = _ldapmodify(ldif)
    if rc != 0:
        return {"ok": False, "error": err or out}
    return {"ok": True, "msg": f"Added '{req.child_group}' to '{req.parent_group}'"}
