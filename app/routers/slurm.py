import subprocess
import re
from fastapi import APIRouter, HTTPException
from app import config
from pydantic import BaseModel
from typing import Optional

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


class NodeActionRequest(BaseModel):
    node: str
    action: str
    reason: Optional[str] = "Admin action via HPC Portal"

@router.post("/node-action")
async def node_action(req: NodeActionRequest):
    if not re.match(r'^[\w\-\.]+$', req.node):
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
    if not re.match(r'^[\w\-\.]+$', req.node):
        raise HTTPException(status_code=400, detail="Invalid node name")
    SCANCEL = f"{config.slurm['bin_path']}/scancel"
    try:
        r = subprocess.run([SCANCEL, "-w", req.node], capture_output=True, text=True, timeout=10)
        return {"ok": r.returncode == 0, "msg": r.stderr.strip() or f"Cancelled all jobs on {req.node}"}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


@router.get("/node/{node_name}")
async def get_node_detail(node_name: str):
    if not re.match(r'^[\w\-\.]+$', node_name):
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
            m = re.match(r'CANCELLED by (\d+)', state)
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
    if not re.match(r'^[\w\.\+\-]+$', job_id):
        raise HTTPException(status_code=400, detail="Invalid job id")
    r = subprocess.run([SCONTROL, "show", "job", job_id, "--details"],
                       capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        raise HTTPException(status_code=404, detail="Job not found")
    raw = r.stdout.strip()
    def g(key):
        m = re.search(rf'(?:^|\s){re.escape(key)}=(\S+)', raw)
        return m.group(1) if m else None
    # stdout/stderr path may contain spaces — grab to end of line
    def gpath(key):
        m = re.search(rf'{re.escape(key)}=(.+)', raw)
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
    if not re.match(r'^[\w\.\+\-]+$', job_id):
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



# ── QoS MANAGEMENT ────────────────────────────────────────────────────────

SACCTMGR = "/opt/slurm/bin/sacctmgr"
SLURM_CONF = "/etc/slurm/slurm.conf"

def _sacctmgr(*args, input_data=None) -> tuple[int, str, str]:
    cmd = [SACCTMGR, "-i"] + list(args)
    try:
        r = subprocess.run(cmd, input=input_data, capture_output=True, text=True, timeout=15)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return 1, "", str(e)


@router.get("/qos")
async def get_qos_list():
    """Return all QoS with full details including Flags and GrpTRES."""
    r = subprocess.run(
        [SACCTMGR, "show", "qos", "-P", "--noheader",
         "format=Name,Priority,MaxWall,MaxJobsPU,MaxSubmitPU,MaxTRESPU,GrpTRES,Flags,PreemptMode"],
        capture_output=True, text=True, timeout=10
    )
    qos_list = []
    for line in r.stdout.strip().splitlines():
        parts = line.split("|")
        if len(parts) < 9:
            continue
        name = parts[0].strip()
        if not name or name == "normal":
            # still include normal
            pass
        # parse MaxTRESPU for gpu
        max_tres_pu = parts[5].strip()
        max_gpus_job = ""
        m = re.search(r'gres/gpu=(\d+)', max_tres_pu)
        if m:
            max_gpus_job = m.group(1)
        # parse GrpTRES for gpu
        grp_tres = parts[6].strip()
        grp_gpus = ""
        m2 = re.search(r'gres/gpu=(\d+)', grp_tres)
        if m2:
            grp_gpus = m2.group(1)
        # parse MaxTRESPU for max_gpus_user (separate from per-job)
        flags = parts[7].strip()
        qos_list.append({
            "name": name,
            "priority": parts[1].strip(),
            "max_wall": parts[2].strip(),
            "max_jobs": parts[3].strip(),
            "max_submit": parts[4].strip(),
            "max_tres_pu": max_tres_pu,
            "max_gpus_job": max_gpus_job,
            "grp_tres": grp_tres,
            "grp_gpus": grp_gpus,
            "flags": flags,
            "preempt_mode": parts[8].strip() if len(parts) > 8 else "",
        })
    return {"qos": qos_list}


class QosCreateRequest(BaseModel):
    name: str
    priority: Optional[str] = ""
    max_wall: Optional[str] = ""
    max_jobs: Optional[str] = ""
    max_submit: Optional[str] = ""
    max_gpus_job: Optional[str] = ""
    grp_gpus: Optional[str] = ""
    flags: Optional[str] = ""

@router.post("/qos/create")
async def create_qos(req: QosCreateRequest):
    if not re.match(r'^[\w\-\.]+$', req.name):
        raise HTTPException(status_code=400, detail="Invalid QoS name")
    parts = [f"Name={req.name}"]
    if req.priority:   parts.append(f"Priority={req.priority}")
    if req.max_wall:   parts.append(f"MaxWall={req.max_wall}")
    if req.max_jobs:   parts.append(f"MaxJobsPerUser={req.max_jobs}")
    if req.max_submit: parts.append(f"MaxSubmitJobsPerUser={req.max_submit}")
    if req.max_gpus_job: parts.append(f"MaxTRESPerUser=gres/gpu={req.max_gpus_job}")
    if req.grp_gpus:   parts.append(f"GrpTRES=gres/gpu={req.grp_gpus}")
    if req.flags:      parts.append(f"Flags={req.flags}")
    rc, out, err = _sacctmgr("add", "qos", *parts)
    if rc != 0:
        return {"ok": False, "msg": err or out}
    return {"ok": True, "msg": f"Created QoS '{req.name}'"}


class QosEditRequest(BaseModel):
    name: str
    priority: Optional[str] = ""
    max_wall: Optional[str] = ""
    max_jobs: Optional[str] = ""
    max_submit: Optional[str] = ""
    max_gpus_job: Optional[str] = ""
    grp_gpus: Optional[str] = ""
    flags: Optional[str] = ""

@router.post("/qos/edit")
async def edit_qos(req: QosEditRequest):
    if not re.match(r'^[\w\-\.]+$', req.name):
        raise HTTPException(status_code=400, detail="Invalid QoS name")
    sets = []
    sets.append(f"Priority={req.priority or '0'}")
    if req.max_wall:   sets.append(f"MaxWall={req.max_wall}")
    else:              sets.append("MaxWall=")
    if req.max_jobs:   sets.append(f"MaxJobsPerUser={req.max_jobs}")
    else:              sets.append("MaxJobsPerUser=")
    if req.max_submit: sets.append(f"MaxSubmitJobsPerUser={req.max_submit}")
    else:              sets.append("MaxSubmitJobsPerUser=")
    sets.append(f"MaxTRESPerUser=gres/gpu={req.max_gpus_job}" if req.max_gpus_job else "MaxTRESPerUser=")
    if req.grp_gpus: sets.append(f"GrpTRES=gres/gpu={req.grp_gpus}")
    if req.flags:      sets.append(f"Flags={req.flags}")
    rc, out, err = _sacctmgr("modify", "qos", f"Name={req.name}", "set", *sets)
    if rc != 0:
        return {"ok": False, "msg": err or out}
    return {"ok": True, "msg": f"Updated QoS '{req.name}'"}


class QosDeleteRequest(BaseModel):
    name: str

@router.post("/qos/delete")
async def delete_qos(req: QosDeleteRequest):
    if not re.match(r'^[\w\-\.]+$', req.name):
        raise HTTPException(status_code=400, detail="Invalid QoS name")
    if req.name in ("normal",):
        return {"ok": False, "msg": "Cannot delete the 'normal' QoS"}
    rc, out, err = _sacctmgr("delete", "qos", f"Name={req.name}")
    if rc != 0:
        return {"ok": False, "msg": err or out}
    return {"ok": True, "msg": f"Deleted QoS '{req.name}'"}


# ── PARTITION QoS MANAGEMENT ──────────────────────────────────────────────

@router.get("/partitions")
async def get_partitions():
    """Return all partitions with their AllowQos and default QoS."""
    r = subprocess.run(
        [SCONTROL, "show", "partition", "-o"],
        capture_output=True, text=True, timeout=10
    )
    partitions = []
    for line in r.stdout.strip().splitlines():
        name_m = re.search(r'PartitionName=(\S+)', line)
        if not name_m:
            continue
        name = name_m.group(1)
        allow_qos_m = re.search(r'AllowQos=(\S+)', line)
        allow_qos = allow_qos_m.group(1) if allow_qos_m else "ALL"
        default_qos_m = re.search(r'\bQoS=(\S+)', line)
        default_qos = default_qos_m.group(1) if default_qos_m else "N/A"
        allow_groups_m = re.search(r'AllowGroups=(\S+)', line)
        allow_groups = allow_groups_m.group(1) if allow_groups_m else "ALL"
        partitions.append({
            "name": name,
            "allow_qos": allow_qos,
            "default_qos": default_qos,
            "allow_groups": allow_groups,
        })
    return {"partitions": partitions}


class PartitionQosRequest(BaseModel):
    partition: str
    allow_qos: str   # comma-separated list or "ALL"
    default_qos: Optional[str] = ""
    write_conf: bool = True

@router.post("/partition/qos")
async def set_partition_qos(req: PartitionQosRequest):
    if not re.match(r'^[\w\-\.]+$', req.partition):
        raise HTTPException(status_code=400, detail="Invalid partition name")

    # Apply live via scontrol
    allow = req.allow_qos.strip() or "ALL"
    cmd = [SCONTROL, "update", f"PartitionName={req.partition}", f"AllowQos={allow}"]
    if req.default_qos:
        cmd.append(f"QoS={req.default_qos}")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        return {"ok": False, "msg": r.stderr.strip() or "scontrol failed"}

    msgs = [f"Live update applied to {req.partition}"]

    if req.write_conf:
        try:
            conf_text = subprocess.run(
                ["sudo", "cat", SLURM_CONF],
                capture_output=True, text=True, timeout=5
            ).stdout

            def _patch_partition(text, part_name, allow_qos_val, def_qos_val):
                lines = text.splitlines(keepends=True)
                in_block = False
                result = []
                i = 0
                while i < len(lines):
                    line = lines[i]
                    # Detect start of our partition block
                    if re.match(rf'\s*PartitionName={re.escape(part_name)}\b', line):
                        in_block = True
                    if in_block:
                        # Remove existing AllowQos and QoS= lines
                        if re.match(r'\s*AllowQos=', line):
                            i += 1
                            continue
                        # Replace QoS= line (partition default qos) if we have a new one
                        if def_qos_val and re.match(r'\s*QoS=', line):
                            # preserve continuation backslash if present
                            has_cont = line.rstrip().endswith('\\')
                            result.append(f"    QoS={def_qos_val}" + (" \\\n" if has_cont else "\n"))
                            i += 1
                            continue
                        # Detect end of block: next PartitionName or empty line without backslash
                        if not line.rstrip().endswith('\\') and not re.match(r'\s*PartitionName=', line):
                            # End of block — insert AllowQos before this line
                            if allow_qos_val and allow_qos_val != "ALL":
                                result.append(f"    AllowQos={allow_qos_val} \\\n")
                            result.append(line)
                            in_block = False
                            i += 1
                            continue
                    result.append(line)
                    i += 1
                return "".join(result)

            new_conf = _patch_partition(conf_text, req.partition, allow, req.default_qos or "")
            wr = subprocess.run(
                ["sudo", "tee", SLURM_CONF],
                input=new_conf, capture_output=True, text=True, timeout=10
            )
            if wr.returncode == 0:
                msgs.append("slurm.conf updated")
            else:
                msgs.append(f"slurm.conf write failed: {wr.stderr.strip()[:80]}")
        except Exception as e:
            msgs.append(f"slurm.conf patch error: {str(e)[:80]}")

    return {"ok": True, "msg": " | ".join(msgs)}
