import subprocess
import base64
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from typing import Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.routers.ad_mgmt import _ad_session, _ldap_run

router = APIRouter()

LDAP_URI  = "ldap://ad.example.local"
LDAP_BASE = "DC=example,DC=local"
MSMTP     = "/usr/bin/msmtp"
FROM_ADDR = "demo-hpc@example.local"


def _get_mail_groups() -> dict:
    """Query AD for all top-level groups under the HPC OU."""
    HPC_OU = "OU=HPC,DC=example,DC=local"
    _require_auth()
    rc, out, err = _ldap_run([
        "ldapsearch", "-H", LDAP_URI, "-x",
        "-D", _ad_session["dn"], "-w", _ad_session["pw"],
        "-b", HPC_OU, "-s", "one",
        "(objectClass=group)", "sAMAccountName"
    ])
    if rc != 0:
        raise HTTPException(status_code=500, detail=f"LDAP error: {err.strip()}")
    lines = []
    for line in out.splitlines():
        if line.startswith(" ") and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    groups = {}
    for line in lines:
        if line.lower().startswith("samaccountname:"):
            sam = line.split(":", 1)[1].strip()
            groups[sam] = sam
    if not groups:
        raise HTTPException(status_code=500, detail="No groups found in HPC OU")
    return groups


def _require_auth():
    if not _ad_session.get("authenticated"):
        raise HTTPException(status_code=401, detail="Not authenticated to AD")


def _unfold(out: str) -> list[str]:
    lines = []
    for line in out.splitlines():
        if line.startswith(" ") and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def _resolve_group_emails(group_name: str, visited: set = None) -> list[str]:
    if visited is None:
        visited = set()
    if group_name in visited:
        return []
    visited.add(group_name)

    rc, out, err = _ldap_run([
        "ldapsearch", "-H", LDAP_URI, "-x",
        "-D", _ad_session["dn"], "-w", _ad_session["pw"],
        "-b", LDAP_BASE, f"(sAMAccountName={group_name})", "member"
    ])
    if rc != 0:
        return []

    lines = _unfold(out)
    member_dns = [l.split(":", 1)[1].strip() for l in lines if l.lower().startswith("member:")]

    emails = []
    for dn in member_dns:
        rc2, out2, _ = _ldap_run([
            "ldapsearch", "-H", LDAP_URI, "-x",
            "-D", _ad_session["dn"], "-w", _ad_session["pw"],
            "-b", dn, "-s", "base", "(objectClass=*)",
            "objectClass", "sAMAccountName", "mail"
        ])
        obj_lines   = _unfold(out2)
        obj_classes = [l.split(":", 1)[1].strip().lower() for l in obj_lines if l.lower().startswith("objectclass:")]
        mail_vals   = [l.split(":", 1)[1].strip() for l in obj_lines if l.lower().startswith("mail:")]
        sam_vals    = [l.split(":", 1)[1].strip() for l in obj_lines if l.lower().startswith("samaccountname:")]

        if "group" in obj_classes:
            if sam_vals:
                emails.extend(_resolve_group_emails(sam_vals[0], visited))
        else:
            emails.extend(mail_vals)

    return emails


@router.get("/groups")
async def get_groups():
    groups = _get_mail_groups()
    return {"groups": [{"id": k, "label": k} for k in groups.keys()]}


class PreviewRequest(BaseModel):
    groups: list[str]


@router.post("/preview-recipients")
async def preview_recipients(req: PreviewRequest):
    _require_auth()
    if not req.groups:
        raise HTTPException(status_code=400, detail="No groups selected")

    all_emails: set[str] = set()
    errors: list[str] = []
    for grp in req.groups:
        try:
            all_emails.update(_resolve_group_emails(grp))
        except Exception as e:
            errors.append(f"{grp}: {e}")

    return {"emails": sorted(all_emails), "count": len(all_emails), "errors": errors}


class SendMailRequest(BaseModel):
    groups:          list[str]
    subject:         str
    body:            str
    attachment_name: Optional[str] = None
    attachment_b64:  Optional[str] = None


@router.post("/send")
async def send_mail(req: SendMailRequest):
    _require_auth()
    if not req.groups:
        raise HTTPException(status_code=400, detail="No groups selected")
    if not req.subject.strip():
        raise HTTPException(status_code=400, detail="Subject is required")
    if not req.body.strip():
        raise HTTPException(status_code=400, detail="Body is required")

    all_emails: set[str] = set()
    resolve_errors: list[str] = []
    for grp in req.groups:
        try:
            all_emails.update(_resolve_group_emails(grp))
        except Exception as e:
            resolve_errors.append(f"{grp}: {e}")

    if not all_emails:
        return {"ok": False, "msg": "No recipients resolved", "errors": resolve_errors}

    recipients = sorted(all_emails)

    msg = MIMEMultipart()
    msg["From"]    = FROM_ADDR
    msg["To"]      = ", ".join(recipients)
    msg["Subject"] = req.subject
    msg.attach(MIMEText(req.body, "plain", "utf-8"))

    if req.attachment_b64 and req.attachment_name:
        try:
            data = base64.b64decode(req.attachment_b64)
            part = MIMEBase("application", "octet-stream")
            part.set_payload(data)
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f'attachment; filename="{req.attachment_name}"')
            msg.attach(part)
        except Exception as e:
            return {"ok": False, "msg": f"Attachment error: {e}"}

    try:
        proc = subprocess.run(
            [MSMTP, "--"] + recipients,
            input=msg.as_string(),
            capture_output=True, text=True, timeout=30
        )
        if proc.returncode != 0:
            return {"ok": False, "msg": proc.stderr.strip() or "msmtp failed", "errors": resolve_errors}
        return {"ok": True, "msg": f"Sent to {len(recipients)} recipients", "recipients": recipients, "errors": resolve_errors}
    except Exception as e:
        return {"ok": False, "msg": str(e), "errors": resolve_errors}
