#!/usr/bin/env python3
"""
Idle-session reaper for interactive (srun/salloc, BatchFlag=0) Slurm jobs.

Runs as a service account on the Slurm controller node. OOD apps (vscode, pycharm, jupyter) submit via
sbatch (BatchFlag=1) and are handled by their own in-job watchdogs; this reaper
deliberately ignores them.

Per poll:
  - enumerate RUNNING BatchFlag=0 jobs
  - on each job's node, list the job's PIDs and their comms (scontrol listpids)
  - "active" = any process beyond the idle-shell baseline (= user is running
    something, GPU or not); "idle" = only baseline processes
  - track idle time per job; warn by email at WARN, scancel at KILL
  - SSH/exec failure for a job => skip it this poll (never reclaim on a reading
    we couldn't take)

WD_ENFORCE=0 => warn-only: logs WOULD-KILL, never scancels. Flip to 1 only after
confirming the service account can scancel other users' jobs and the logs look right.
"""

import os
import re
import sys
import time
import shlex
import smtplib
import subprocess
from email.message import EmailMessage

# ---------------- Tunables ----------------
POLL      = 60      # seconds between polls
WARN      = 1800    # idle seconds before warning email (30 min)
KILL      = 2700    # idle seconds before scancel (45 min)
ENFORCE   = 0       # 0 = warn-only (log WOULD-KILL); 1 = actually scancel
SSH_TO    = 10      # ssh ConnectTimeout seconds

SLURM_BIN = "/opt/slurm/bin"
SCONTROL  = f"{SLURM_BIN}/scontrol"
SCANCEL   = f"{SLURM_BIN}/scancel"
SQUEUE    = f"{SLURM_BIN}/squeue"

SMTP_HOST = "smtp.example.local"
SMTP_PORT = 25
MAIL_FROM = "hpc@example.local"

# Activity state feed (read by the HPC portal jobs view).
# Same format/dir as the OOD in-job watchdogs; one file per job, mode 644.
STATE_DIR = "/shared/idle-watchdog/state"
MECH      = "srun-reaper"


def write_state(jid, status, idle_seconds, phase, node, now):
    """Atomically publish a job's activity state. Never raises."""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        path = os.path.join(STATE_DIR, f"{jid}.json")
        tmp  = os.path.join(STATE_DIR, f".{jid}.{os.getpid()}.tmp")
        payload = (
            '{"job_id":"%s","status":"%s","idle_seconds":%d,"phase":"%s",'
            '"mechanism":"%s","host":"%s","updated":%d}\n'
            % (jid, status, int(idle_seconds), phase, MECH, node, int(now))
        )
        with open(tmp, "w") as f:
            f.write(payload)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except Exception as e:
        log(f"write_state {jid} failed: {e}")
        try:
            os.remove(tmp)
        except Exception:
            pass


def remove_state(jid):
    """Drop a job's state file (job gone or cancelled). Never raises."""
    try:
        os.remove(os.path.join(STATE_DIR, f"{jid}.json"))
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f"remove_state {jid} failed: {e}")

# Processes present in an idle interactive shell (plain or apptainer-wrapped).
# Anything outside this set == the user is doing something.
BASELINE = {
    "slurmstepd", "bash", "sh", "dash",
    "starter", "starter-suid", "squashfuse_ll", "apptainer",
    "sleep", "srun",
}

STUDENT_GROUPS = {"students", "course_students"}


def log(msg):
    print(f"[{time.strftime('%F %T')}] {msg}", flush=True)


def run(cmd, timeout=30):
    """Run a local command list; return (rc, stdout). Never raises."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout
    except Exception as e:
        log(f"run error {cmd!r}: {e}")
        return 255, ""


def list_interactive_jobs():
    """Return list of dicts: {jobid, node, user} for RUNNING BatchFlag=0 jobs."""
    rc, out = run([SCONTROL, "-o", "show", "job"])
    if rc != 0 or not out:
        return []
    jobs = []
    for line in out.splitlines():
        if "JobState=RUNNING" not in line or "BatchFlag=0" not in line:
            continue
        fields = dict(re.findall(r"(\w+)=(\S+)", line))
        jid = fields.get("JobId")
        nodelist = fields.get("NodeList", "")
        userid = fields.get("UserId", "")          # e.g. alice(1000001)
        if not jid or not nodelist or nodelist in ("(null)", "None"):
            continue
        user = userid.split("(", 1)[0] if userid else ""
        node = first_host(nodelist)
        if node:
            jobs.append({"jobid": jid, "node": node, "user": user})
    return jobs


def first_host(nodelist):
    """Resolve a NodeList (possibly a range) to its first hostname."""
    if "[" not in nodelist and "," not in nodelist:
        return nodelist
    rc, out = run([SCONTROL, "show", "hostnames", nodelist])
    if rc == 0 and out.strip():
        return out.split()[0]
    return ""


def job_is_active(node, jobid):
    """
    On `node`, list the job's process comms.
    Returns True (active), False (idle), or None (could not determine -> skip).
    """
    remote = (
        f"{SCONTROL} listpids {jobid} "
        f"| awk 'NR>1 && $1>0 {{print $1}}' "
        f"| xargs -r ps -o comm= -p"
    )
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={SSH_TO}",
           node, remote]
    rc, out = run(cmd, timeout=SSH_TO + 20)
    if rc == 255:            # ssh-level failure: unknown, do not decide
        log(f"ssh failed to {node} for job {jobid}; skipping")
        return None
    comms = [c.strip() for c in out.splitlines() if c.strip()]
    if not comms:            # job likely just ended; treat as not-active
        return False
    return any(c not in BASELINE for c in comms)


def user_email(user):
    dom = "example.local"
    rc, out = run(["id", "-nG", user])
    if rc == 0 and (set(out.split()) & STUDENT_GROUPS):
        dom = "students.example.local"
    return f"{user}@{dom}"


def notify(user, subject, body):
    to = user_email(user)
    log(f"NOTIFY to={to} :: {subject}")
    try:
        m = EmailMessage()
        m["From"] = MAIL_FROM
        m["To"] = to
        m["Subject"] = subject
        m.set_content(body)
        s = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10)
        try:
            s.send_message(m)
        finally:
            s.quit()
    except Exception as e:
        log(f"mail send failed to {to}: {e}")


def scancel(jobid):
    rc, _ = run([SCANCEL, jobid])
    log(f"scancel {jobid} rc={rc}")


def main():
    log(f"idle-reaper start warn={WARN}s kill={KILL}s enforce={ENFORCE} poll={POLL}s")
    last_active = {}   # jobid -> epoch of last observed activity
    warned = set()     # jobids already warned this idle spell

    while True:
        time.sleep(POLL)
        now = int(time.time())
        jobs = list_interactive_jobs()
        live = {j["jobid"] for j in jobs}

        # forget jobs that are gone
        for jid in list(last_active):
            if jid not in live:
                last_active.pop(jid, None)
                warned.discard(jid)
                remove_state(jid)

        for j in jobs:
            jid, node, user = j["jobid"], j["node"], j["user"]
            last_active.setdefault(jid, now)   # seed grace on first sight

            active = job_is_active(node, jid)
            if active is None:
                continue                        # couldn't read; never reclaim
            if active:
                last_active[jid] = now
                warned.discard(jid)
                write_state(jid, "active", 0, "BUSY", node, now)
                continue

            idle = now - last_active[jid]

            if idle >= KILL:
                if ENFORCE == 1:
                    notify(user,
                           f"HPC: idle session reclaimed (job {jid})",
                           f"Your interactive session (job {jid} on {node}) was idle "
                           f"for over {KILL//60} minutes with no activity and has been "
                           f"cancelled to free resources. Start a new session whenever "
                           f"you need it.")
                    log(f"KILL job={jid} node={node} user={user} idle={idle}s")
                    scancel(jid)
                    last_active.pop(jid, None)
                    warned.discard(jid)
                    remove_state(jid)
                else:
                    log(f"WOULD-KILL job={jid} node={node} user={user} "
                        f"idle={idle}s (enforce=0)")
                    write_state(jid, "would-kill", idle, "IDLE", node, now)
            elif idle >= WARN and jid not in warned:
                notify(user,
                       f"HPC: idle session warning (job {jid})",
                       f"Your interactive session (job {jid} on {node}) has been idle "
                       f"for about {idle//60} minutes. It will be cancelled at "
                       f"{KILL//60} minutes idle to free resources. Run anything to "
                       f"reset the timer.")
                warned.add(jid)
                log(f"WARN job={jid} node={node} user={user} idle={idle}s")
                write_state(jid, "warned", idle, "IDLE", node, now)
            elif idle >= WARN:
                # already warned this spell; keep state fresh
                write_state(jid, "warned", idle, "IDLE", node, now)
            else:
                write_state(jid, "active", idle, "IDLE", node, now)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        log(f"fatal: {e}")
        sys.exit(1)
