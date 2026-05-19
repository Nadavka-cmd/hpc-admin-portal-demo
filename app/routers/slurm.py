import subprocess
import re
from fastapi import APIRouter, HTTPException
from app import config

router = APIRouter()

SINFO    = f"{config.slurm['bin_path']}/sinfo"
SQUEUE   = f"{config.slurm['bin_path']}/squeue"
SACCT    = f"{config.slurm['bin_path']}/sacct"
SCONTROL = f"{config.slurm['bin_path']}/scontrol"


def run_cmd(cmd: list[str]) -> str:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            raise HTTPException(status_code=500, detail=result.stderr.strip())
        return result.stdout.strip()
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail=f"Command not found: {cmd[0]}")
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Command timed out")


def _parse_gres_gpu(tres_str: str) -> str:
    m = re.search(r'gres/gpu(?::[^=]+)?=(\d+)', tres_str)
    if m:
        return f"gpu:{m.group(1)}"
    return ""


def _fetch_sacct_gpu(job_ids: list[str]) -> dict[str, str]:
    if not job_ids:
        return {}
    try:
        ids = ",".join(job_ids)
        r = subprocess.run(
            [SACCT, "-j", ids, "--format=JobID,AllocTRES", "--noheader", "-P",
             "--state=RUNNING", "-X"],
            capture_output=True, text=True, timeout=10
        )
        result = {}
        for line in r.stdout.splitlines():
            parts = line.split("|", 1)
            if len(parts) == 2:
                job_id = parts[0].strip()
                tres   = parts[1].strip()
                gpu    = _parse_gres_gpu(tres)
                if gpu:
                    result[job_id] = gpu
        return result
    except Exception:
        return {}


@router.get("/nodes")
async def get_nodes():
    output = run_cmd([SINFO, "-o", "%n %T %G %C %m", "--noheader"])
    nodes = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 5:
            nodes.append({
                "node":   parts[0],
                "state":  parts[1],
                "gres":   parts[2],
                "cpus":   parts[3],
                "memory": parts[4],
            })
    return {"nodes": nodes}


@router.get("/jobs")
async def get_jobs():
    output = run_cmd([
        SQUEUE, "--noheader",
        "-o", "%i|%u|%a|%j|%T|%M|%l|%N|%R|%b"
    ])
    jobs = []
    job_ids_no_gpu = []

    for line in output.splitlines():
        parts = line.split("|", 9)
        if len(parts) < 9:
            continue
        gres_raw = parts[9].strip() if len(parts) > 9 else ""
        gres = "" if gres_raw in ("N/A", "(null)", "null", "0", "") else gres_raw
        job_id = parts[0].strip()
        jobs.append({
            "job_id":     job_id,
            "user":       parts[1].strip(),
            "account":    parts[2].strip(),
            "name":       parts[3].strip(),
            "state":      parts[4].strip(),
            "time_run":   parts[5].strip(),
            "time_limit": parts[6].strip(),
            "nodes":      parts[7].strip(),
            "reason":     parts[8].strip(),
            "gres":       gres,
        })
        if not gres and parts[4].strip() == "RUNNING":
            job_ids_no_gpu.append(job_id)

    if job_ids_no_gpu:
        sacct_gpu = _fetch_sacct_gpu(job_ids_no_gpu)
        for job in jobs:
            if job["job_id"] in sacct_gpu:
                job["gres"] = sacct_gpu[job["job_id"]]

    return {"jobs": jobs}


from pydantic import BaseModel
from typing import Optional
import re as _re

class NodeActionRequest(BaseModel):
    node: str
    action: str
    reason: Optional[str] = "Admin action via HPC Portal"

@router.post("/node-action")
async def node_action(req: NodeActionRequest):
    if not _re.match(r'^[\w\-\.]+$', req.node):
        raise HTTPException(status_code=400, detail="Invalid node name")

    reason = req.reason or "Admin action via HPC Portal"
    actions = {
        "drain":      [SCONTROL, "update", f"NodeName={req.node}", "State=DRAIN",     f"Reason={reason}"],
        "resume":     [SCONTROL, "update", f"NodeName={req.node}", "State=RESUME"],
        "down":       [SCONTROL, "update", f"NodeName={req.node}", "State=DOWN",      f"Reason={reason}"],
        "undrain":    [SCONTROL, "update", f"NodeName={req.node}", "State=RESUME"],
        "power_down": [SCONTROL, "update", f"NodeName={req.node}", "State=POWER_DOWN"],
    }
    if req.action not in actions:
        raise HTTPException(status_code=400, detail="Invalid action")

    cmd = actions[req.action]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return {"ok": False, "msg": r.stderr.strip() or r.stdout.strip()}
        return {"ok": True, "msg": f"{req.action} applied to {req.node}"}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


@router.post("/scancel-node")
async def scancel_node(req: NodeActionRequest):
    if not _re.match(r'^[\w\-\.]+$', req.node):
        raise HTTPException(status_code=400, detail="Invalid node name")
    SCANCEL = f"{config.slurm['bin_path']}/scancel"
    try:
        r = subprocess.run([SCANCEL, "-w", req.node], capture_output=True, text=True, timeout=10)
        return {"ok": r.returncode == 0, "msg": r.stderr.strip() or f"Cancelled all jobs on {req.node}"}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


@router.get("/node/{node_name}")
async def get_node_detail(node_name: str):
    if not _re.match(r'^[\w\-\.]+$', node_name):
        raise HTTPException(status_code=400, detail="Invalid node name")
    r = subprocess.run([SCONTROL, "show", "node", node_name],
                       capture_output=True, text=True, timeout=10)
    return {"output": r.stdout.strip(), "node": node_name}


@router.get("/jobs/recent-finished")
async def get_recent_finished_jobs(limit: int = 10):
    try:
        r = subprocess.run([
            "sudo", SACCT, "-n", "-P", "-X",
            "--format=JobID,User,Account,JobName,State,ExitCode,Elapsed,NodeList"
        ], capture_output=True, text=True, timeout=15)
        jobs = []
        for line in r.stdout.strip().splitlines():
            parts = line.split("|")
            if len(parts) < 8:
                continue
            state = parts[4].strip()
            if state == "TIMEOUT":
                state = "COMPLETED (TIMEOUT)"
            if any(s in state for s in ["RUNNING", "PENDING", "RESIZING", "SUSPENDED"]):
                continue
            # Resolve "CANCELLED by <uid>" to "CANCELLED by <username>"
            import re as _re
            m = _re.match(r'CANCELLED by (\d+)', state)
            if m:
                uid = m.group(1)
                try:
                    gr = subprocess.run(["getent", "passwd", uid], capture_output=True, text=True, timeout=3)
                    if gr.returncode == 0 and gr.stdout:
                        uname = gr.stdout.split(":")[0]
                        state = f"CANCELLED by {uname}"
                except Exception:
                    pass
            jobs.append({
                "job_id":   parts[0].strip(),
                "user":     parts[1].strip(),
                "account":  parts[2].strip(),
                "name":     parts[3].strip(),
                "state":    state,
                "exitcode": parts[5].strip(),
                "elapsed":  parts[6].strip(),
                "nodes":    parts[7].strip(),
            })
        return {"jobs": list(reversed(jobs))[:limit]}
    except Exception as e:
        return {"jobs": [], "error": str(e)}


@router.get("/jobs/{job_id}")
async def get_job_detail(job_id: str):
    import re as _re2
    if not _re2.match(r'^[\w\.\+\-]+$', job_id):
        raise HTTPException(status_code=400, detail="Invalid job id")
    r = subprocess.run([SCONTROL, "show", "job", job_id, "--details"],
                       capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        raise HTTPException(status_code=404, detail="Job not found")
    raw = r.stdout.strip()
    def g(key):
        m = _re2.search(rf'(?:^|\s){_re2.escape(key)}=(\S+)', raw)
        return m.group(1) if m else None
    # stdout/stderr path may contain spaces — grab to end of line
    def gpath(key):
        m = _re2.search(rf'{_re2.escape(key)}=(.+)', raw)
        return m.group(1).strip() if m else None
    return {
        "job_id":      job_id,
        "job_name":    g("JobName"),
        "user":        g("UserId"),
        "account":     g("Account"),
        "partition":   g("Partition"),
        "state":       g("JobState"),
        "reason":      g("Reason"),
        "num_nodes":   g("NumNodes"),
        "num_cpus":    g("NumCPUs"),
        "node_list":   g("NodeList"),
        "time_limit":  g("TimeLimit"),
        "run_time":    g("RunTime"),
        "submit_time": g("SubmitTime"),
        "start_time":  g("StartTime"),
        "end_time":    g("EndTime"),
        "mem_per_node":g("MinMemoryNode"),
        "gres":        g("JOB_GRES"),
        "work_dir":    gpath("WorkDir"),
        "std_out":     gpath("StdOut"),
        "std_err":     gpath("StdErr"),
        "cluster":     g("ClusterName") or "ECE HPC Cluster",
        "raw":         raw,
    }


class JobActionRequest(BaseModel):
    action: str  # cancel | requeue | hold | release

@router.post("/jobs/{job_id}/action")
async def job_action(job_id: str, req: JobActionRequest):
    import re as _re3
    if not _re3.match(r'^[\w\.\+\-]+$', job_id):
        raise HTTPException(status_code=400, detail="Invalid job id")
    SCANCEL  = f"{config.slurm['bin_path']}/scancel"
    SCONTROL2 = f"{config.slurm['bin_path']}/scontrol"
    action = req.action.lower()
    try:
        if action == "cancel":
            cmd = [SCANCEL, job_id]
            ok_msg = f"Job {job_id} cancelled"
        elif action == "requeue":
            cmd = [SCONTROL2, "requeue", job_id]
            ok_msg = f"Job {job_id} requeued"
        elif action == "hold":
            cmd = [SCONTROL2, "hold", job_id]
            ok_msg = f"Job {job_id} held"
        elif action == "release":
            cmd = [SCONTROL2, "release", job_id]
            ok_msg = f"Job {job_id} released"
        else:
            raise HTTPException(status_code=400, detail="Unknown action")
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return {"ok": r.returncode == 0, "msg": r.stderr.strip() or ok_msg}
    except HTTPException:
        raise
    except Exception as e:
        return {"ok": False, "msg": str(e)}

