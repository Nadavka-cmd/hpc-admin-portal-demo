from typing import Optional
import subprocess
import hashlib
import re
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()

def _get_ssh_user():
    import yaml
    from pathlib import Path
    cfg = yaml.safe_load(open(Path(__file__).resolve().parent.parent.parent / "config.yaml"))
    return cfg.get("ssh",{}).get("user","profadmin")

SSH_USER = _get_ssh_user()
MASTER   = "hpc-master"
SINFO    = "/opt/slurm/bin/sinfo"
SCONTROL = "/opt/slurm/bin/scontrol"

TRACKED_FILES = [
    ("/etc/slurm/slurm.conf",     "/etc/slurm/slurm.conf",     "slurm.conf",  "Slurm main config"),
    ("/etc/sssd/sssd.conf",       "/etc/sssd/sssd.conf",        "sssd.conf",   "SSSD / AD auth"),
    ("/etc/hosts",                "/etc/hosts",                 "hosts",       "Hosts file"),
    ("/etc/security/limits.conf", "/etc/security/limits.conf",  "limits",      "PAM limits"),
    ("/etc/sysctl.conf",          "/etc/sysctl.conf",           "sysctl",      "Kernel params"),
    ("/etc/environment",          "/etc/environment",           "environ",     "Env / proxy vars"),
]

HOSTS_BEGIN = "# BEGIN ANSIBLE MANAGED CLUSTER HOSTS"
HOSTS_END   = "# END ANSIBLE MANAGED CLUSTER HOSTS"


def _ssh(node: str, cmd: str, timeout: int = 15) -> tuple[int, str, str]:
    r = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=5",
         "-o", "BatchMode=yes", f"{SSH_USER}@{node}", "bash"],
        input=cmd, capture_output=True, text=True, timeout=timeout,
    )
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def _scp(local: str, node: str, remote: str) -> tuple[bool, str]:
    """SCP via tmp file to avoid permission issues."""
    tmp = f"/tmp/.hpc_sync_{os.path.basename(remote)}"

    # Make local readable if needed
    readable = local
    tmp_local = f"/tmp/.hpc_local_{os.path.basename(local)}"
    try:
        open(local, "rb").close()
    except PermissionError:
        r = subprocess.run(["sudo", "cp", local, tmp_local], capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return False, f"can't read {local}"
        subprocess.run(["sudo", "chmod", "644", tmp_local], capture_output=True, timeout=5)
        readable = tmp_local

    r = subprocess.run(
        ["scp", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=5",
         "-o", "BatchMode=yes", readable, f"{SSH_USER}@{node}:{tmp}"],
        capture_output=True, text=True, timeout=30,
    )
    if readable != local:
        subprocess.run(["sudo", "rm", "-f", tmp_local], capture_output=True, timeout=5)
    if r.returncode != 0:
        return False, r.stderr.strip()[:120]

    FILE_META = {
        "/etc/slurm/slurm.conf":     ("slurm:slurm", "644"),
        "/etc/sssd/sssd.conf":       ("root:root",   "600"),
        "/etc/hosts":                ("root:root",   "644"),
        "/etc/security/limits.conf": ("root:root",   "644"),
        "/etc/sysctl.conf":          ("root:root",   "644"),
        "/etc/environment":          ("root:root",   "644"),
    }
    owner, mode = FILE_META.get(remote, ("root:root", "644"))
    cmd = (f"sudo mv {tmp} {remote} "
           f"&& sudo chown {owner} {remote} "
           f"&& sudo chmod {mode} {remote} "
           f"|| {{ sudo rm -f {tmp}; exit 1; }}")
    rc, _, err = _ssh(node, cmd)
    return (True, "") if rc == 0 else (False, err[:120])


def _local_md5(path: str) -> Optional[str]:
    try:
        h = hashlib.md5()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except PermissionError:
        try:
            r = subprocess.run(["sudo", "md5sum", path], capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                return r.stdout.strip().split()[0]
        except Exception:
            pass
        return None
    except Exception:
        return None


def _read_file(path: str) -> Optional[str]:
    try:
        return Path(path).read_text(errors="replace")
    except PermissionError:
        try:
            r = subprocess.run(["sudo", "cat", path], capture_output=True, text=True, timeout=5)
            return r.stdout if r.returncode == 0 else None
        except Exception:
            return None
    except Exception:
        return None


def _check_hosts(node: str) -> dict:
    master_text = _read_file("/etc/hosts")
    if not master_text:
        return {"status": "ERROR", "detail": "can't read master /etc/hosts"}
    if HOSTS_BEGIN not in master_text:
        return {"status": "SKIP", "detail": "no Ansible block on master"}
    if node == MASTER:
        return {"status": "OK", "detail": "master"}
    try:
        rc, out, err = _ssh(node, "cat /etc/hosts", timeout=10)
        if rc != 0:
            return {"status": "ERROR", "detail": f"SSH rc={rc}"}
    except subprocess.TimeoutExpired:
        return {"status": "ERROR", "detail": "SSH timeout"}
    if HOSTS_BEGIN not in out:
        return {"status": "MISSING", "detail": "no Ansible block"}
    # Compare Ansible blocks
    def _block(text):
        try:
            s = text.index(HOSTS_BEGIN) + len(HOSTS_BEGIN)
            e = text.index(HOSTS_END)
            return set(l.strip() for l in text[s:e].splitlines() if l.strip() and not l.strip().startswith("#"))
        except ValueError:
            return set()
    master_e = _block(master_text)
    node_e   = _block(out)
    missing  = master_e - node_e
    extra    = node_e - master_e
    if not missing and not extra:
        return {"status": "OK", "detail": f"{len(master_e)} entries match"}
    parts = []
    if missing: parts.append(f"missing {len(missing)}")
    if extra:   parts.append(f"extra {len(extra)}")
    return {"status": "MISMATCH", "detail": ", ".join(parts)}


def _check_file(local_path: str, node: str, remote_path: str) -> dict:
    if local_path == "/etc/hosts":
        return _check_hosts(node)
    # Check file exists on master — handle PermissionError (file exists but not readable by profadmin)
    try:
        exists = Path(local_path).exists()
    except PermissionError:
        exists = True  # file exists, just not readable directly
    if not exists:
        try:
            r = subprocess.run(["sudo", "test", "-f", local_path], capture_output=True, timeout=3)
            if r.returncode != 0:
                return {"status": "SKIP", "detail": "not on master"}
        except Exception:
            return {"status": "SKIP", "detail": "not on master"}
    master_md5 = _local_md5(local_path)
    if master_md5 is None:
        return {"status": "ERROR", "detail": "can't read master file"}
    if node == MASTER:
        return {"status": "OK", "detail": "master"}
    use_sudo = "sssd" in remote_path
    if use_sudo:
        cmd = f"if sudo test -f {remote_path}; then sudo md5sum {remote_path} 2>/dev/null; else echo __MISSING__; fi"
    else:
        cmd = f"test -f {remote_path} && md5sum {remote_path} 2>/dev/null || echo __MISSING__"
    try:
        rc, out, err = _ssh(node, cmd, timeout=10)
        if rc != 0 and "__MISSING__" not in out:
            return {"status": "ERROR", "detail": f"SSH rc={rc} {err[:30]}"}
        if "__MISSING__" in out:
            return {"status": "MISSING", "detail": "absent on node"}
        m = re.search(r"^([0-9a-f]{32})\s", out, re.MULTILINE)
        if not m:
            return {"status": "ERROR", "detail": f"unexpected output"}
        node_md5 = m.group(1)
        match = master_md5 == node_md5
        return {"status": "OK" if match else "MISMATCH",
                "detail": "match" if match else f"master:{master_md5[:8]} node:{node_md5[:8]}"}
    except subprocess.TimeoutExpired:
        return {"status": "ERROR", "detail": "SSH timeout"}
    except Exception as e:
        return {"status": "ERROR", "detail": str(e)[:40]}


def _discover_nodes() -> list[str]:
    try:
        r = subprocess.run([SINFO, "-h", "-o", "%N", "--noheader"],
                           capture_output=True, text=True, timeout=8)
        if r.returncode != 0:
            return []
        nodes = set()
        for line in r.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            r2 = subprocess.run([SCONTROL, "show", "hostnames", line],
                                capture_output=True, text=True, timeout=5)
            for n in r2.stdout.splitlines():
                n = n.strip()
                if n:
                    nodes.add(n)
        return sorted(nodes)
    except Exception:
        return []


def _check_node_all_files(node: str) -> dict:
    results = {}
    for local_path, remote_path, short, _ in TRACKED_FILES:
        results[short] = _check_file(local_path, node, remote_path)
    return {"node": node, "files": results}


# ── API endpoints ──────────────────────────────────────────────────────────

@router.get("/nodes")
async def get_sync_nodes():
    """Discover all nodes for config sync."""
    nodes = _discover_nodes()
    all_nodes = [MASTER] + [n for n in nodes if n != MASTER]
    return {
        "nodes": all_nodes,
        "files": [{"short": s, "path": lp, "desc": d} for lp, _, s, d in TRACKED_FILES]
    }


@router.get("/check-all")
async def check_all():
    """Check all files on all nodes in parallel."""
    nodes = _discover_nodes()
    all_nodes = [MASTER] + [n for n in nodes if n != MASTER]
    results = []
    with ThreadPoolExecutor(max_workers=12) as ex:
        futures = {ex.submit(_check_node_all_files, n): n for n in all_nodes}
        for f in as_completed(futures):
            results.append(f.result())
    results.sort(key=lambda x: (x["node"] != MASTER, x["node"]))
    return {
        "nodes": results,
        "files": [{"short": s, "path": lp, "desc": d} for lp, _, s, d in TRACKED_FILES]
    }


@router.get("/diff/{node}/{file_short}")
async def get_diff(node: str, file_short: str):
    """Get unified diff between master and node for a specific file."""
    if not re.match(r'^[\w\-\.]+$', node) or not re.match(r'^[\w\.]+$', file_short):
        raise HTTPException(status_code=400, detail="Invalid parameters")
    entry = next((x for x in TRACKED_FILES if x[2] == file_short), None)
    if not entry:
        raise HTTPException(status_code=404, detail="File not tracked")
    local_path, remote_path, short, desc = entry
    master_text = _read_file(local_path)
    if master_text is None:
        return {"diff": None, "error": "Can't read master file", "master": None, "node_text": None}
    if node == MASTER:
        return {"diff": "", "error": None, "master": master_text, "node_text": master_text}
    try:
        rc, node_text, err = _ssh(node, f"{'sudo ' if 'sssd' in remote_path else ''}cat {remote_path} 2>/dev/null || echo __MISSING__", timeout=10)
        if "__MISSING__" in node_text:
            return {"diff": None, "error": "File missing on node", "master": master_text, "node_text": None}
    except Exception as e:
        return {"diff": None, "error": str(e), "master": master_text, "node_text": None}

    # Generate unified diff
    import difflib
    master_lines = master_text.splitlines(keepends=True)
    node_lines   = node_text.splitlines(keepends=True)
    diff = list(difflib.unified_diff(
        master_lines, node_lines,
        fromfile=f"master:{local_path}",
        tofile=f"{node}:{remote_path}",
        lineterm=""
    ))
    return {"diff": "".join(diff) if diff else "", "error": None,
            "master": master_text, "node_text": node_text}


class SyncRequest(BaseModel):
    node: str
    file_short: str


class SyncAllRequest(BaseModel):
    targets: list[dict]  # [{node, file_short}]


@router.post("/push")
async def push_file(req: SyncRequest):
    """Push a single file to a single node."""
    if not re.match(r'^[\w\-\.]+$', req.node):
        raise HTTPException(status_code=400, detail="Invalid node")
    entry = next((x for x in TRACKED_FILES if x[2] == req.file_short), None)
    if not entry:
        raise HTTPException(status_code=404, detail="File not tracked")
    local_path, remote_path, short, _ = entry
    ok, err = _scp(local_path, req.node, remote_path)

    # Post-push actions
    msgs = []
    if ok:
        if "slurm" in remote_path:
            rc, _, e = _ssh(req.node, "sudo /opt/slurm/bin/scontrol reconfigure 2>/dev/null || true", timeout=15)
            msgs.append(f"{'✓' if rc==0 else '⚠'} scontrol reconfigure")
        if "sssd" in remote_path:
            rc, _, e = _ssh(req.node, "sudo systemctl restart sssd 2>/dev/null || true", timeout=15)
            msgs.append(f"{'✓' if rc==0 else '⚠'} sssd restarted")

    return {"ok": ok, "error": err, "messages": msgs,
            "node": req.node, "file": short}


@router.post("/push-all-mismatches")
async def push_all_mismatches(req: SyncAllRequest):
    """Push multiple file/node combinations."""
    results = []
    for target in req.targets:
        node = target.get("node", "")
        short = target.get("file_short", "")
        if not re.match(r'^[\w\-\.]+$', node):
            results.append({"ok": False, "node": node, "file": short, "error": "Invalid node"})
            continue
        entry = next((x for x in TRACKED_FILES if x[2] == short), None)
        if not entry:
            results.append({"ok": False, "node": node, "file": short, "error": "Unknown file"})
            continue
        local_path, remote_path, _, _ = entry
        ok, err = _scp(local_path, node, remote_path)
        results.append({"ok": ok, "node": node, "file": short, "error": err})

    # Post-push: scontrol reconfigure once if any slurm.conf pushed
    if any(r["file"] == "slurm.conf" and r["ok"] for r in results):
        subprocess.run(["/opt/slurm/bin/scontrol", "reconfigure"],
                       capture_output=True, timeout=10)

    return {"results": results}


# ── HOSTS BLOCK EDITOR ────────────────────────────────────────────────────

HOSTS_FILE = "/etc/hosts"

@router.get("/hosts-block")
async def get_hosts_block():
    """Read the Ansible-managed block from /etc/hosts on master."""
    text = _read_file(HOSTS_FILE)
    if text is None:
        raise HTTPException(status_code=500, detail="Cannot read /etc/hosts")
    if HOSTS_BEGIN not in text or HOSTS_END not in text:
        raise HTTPException(status_code=404, detail="Ansible block markers not found in /etc/hosts")
    s = text.index(HOSTS_BEGIN) + len(HOSTS_BEGIN)
    e = text.index(HOSTS_END)
    block = text[s:e].strip("\n")
    return {"block": block}


class HostsBlockRequest(BaseModel):
    block: str


@router.post("/hosts-block")
async def save_hosts_block(req: HostsBlockRequest):
    """Replace the Ansible-managed block in /etc/hosts on master."""
    text = _read_file(HOSTS_FILE)
    if text is None:
        raise HTTPException(status_code=500, detail="Cannot read /etc/hosts")
    if HOSTS_BEGIN not in text or HOSTS_END not in text:
        raise HTTPException(status_code=404, detail="Ansible block markers not found")

    s = text.index(HOSTS_BEGIN)
    e = text.index(HOSTS_END) + len(HOSTS_END)
    new_block = req.block.strip("\n")
    new_text = text[:s] + HOSTS_BEGIN + "\n" + new_block + "\n" + HOSTS_END + text[e:]

    # Write via sudo tee
    r = subprocess.run(
        ["sudo", "tee", HOSTS_FILE],
        input=new_text, capture_output=True, text=True, timeout=10
    )
    if r.returncode != 0:
        raise HTTPException(status_code=500, detail=r.stderr.strip() or "Write failed")

    return {"ok": True, "msg": "Hosts block saved to /etc/hosts on master"}


# ── GENERIC FILE EDITOR ───────────────────────────────────────────────────

EDITABLE_FILES = {
    "slurm.conf": "/etc/slurm/slurm.conf",
}

class FileContentRequest(BaseModel):
    content: str

@router.get("/file/{file_key}")
async def get_file(file_key: str):
    if file_key not in EDITABLE_FILES:
        raise HTTPException(status_code=404, detail="File not editable via portal")
    path = EDITABLE_FILES[file_key]
    text = _read_file(path)
    if text is None:
        raise HTTPException(status_code=500, detail=f"Cannot read {path}")
    return {"content": text, "path": path}

@router.post("/file/{file_key}")
async def save_file(file_key: str, req: FileContentRequest):
    if file_key not in EDITABLE_FILES:
        raise HTTPException(status_code=404, detail="File not editable via portal")
    path = EDITABLE_FILES[file_key]
    r = subprocess.run(
        ["sudo", "tee", path],
        input=req.content, capture_output=True, text=True, timeout=10
    )
    if r.returncode != 0:
        raise HTTPException(status_code=500, detail=r.stderr.strip() or "Write failed")
    return {"ok": True, "msg": f"Saved {path} on hpc-master"}
