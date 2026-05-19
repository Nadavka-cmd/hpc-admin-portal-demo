import subprocess
import json
import urllib.request
import urllib.parse
import re
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()

# ── constants (will move to config.ini later) ──────────────────────────────
SCRATCH_PATH   = "/scratch"
PROMETHEUS     = "http://prometheus.example.local:9090"
WARN_DAYS      = 7
STALE_DAYS     = 30
SKIP_NAMES     = {"lost+found"}
SSH_USER       = "demo-admin"
SINFO_BIN      = "/opt/slurm/bin/sinfo"
SQUEUE_BIN     = "/opt/slurm/bin/squeue"

STORAGE_NODES  = ["storage-a", "storage-b"]
BACKUP_NODES   = ["storage-backup"]

IGNORE_MOUNTS  = {"/boot/efi", "/boot", "/run", "/opt/sentinelone/rpm_mount"}
IGNORE_FSTYPES = {"tmpfs", "vfat"}
NFS_MOUNTS     = {"/demo/home", "/demo/sif_images", "/demo/datasets", "/demo/projects"}


# ── helpers ────────────────────────────────────────────────────────────────

def _ssh(node: str, cmd: str, timeout: int = 30, login: bool = False) -> tuple[int, str, str]:
    """Run cmd on node via SSH. Script is passed via stdin to avoid quoting issues."""
    shell = ["bash", "-l"] if login else ["bash"]
    r = subprocess.run(
        ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=5",
         "-o", "BatchMode=yes",
         f"{SSH_USER}@{node}"] + shell,
        input=cmd, capture_output=True, text=True, timeout=timeout,
    )
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def _fmt_bytes(b: int) -> str:
    if b >= 1_099_511_627_776:
        return f"{b/1_099_511_627_776:.1f} TB"
    if b >= 1_073_741_824:
        return f"{b/1_073_741_824:.1f} GB"
    if b >= 1_048_576:
        return f"{b/1_048_576:.1f} MB"
    if b >= 1_024:
        return f"{b/1_024:.1f} KB"
    return f"{b} B"


def _prom_query(query: str) -> list[dict]:
    try:
        url    = f"{PROMETHEUS}/api/v1/query"
        params = urllib.parse.urlencode({"query": query})
        req    = urllib.request.Request(f"{url}?{params}")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=6) as resp:
            data = json.loads(resp.read())
        if data.get("status") == "success":
            return data["data"]["result"]
    except Exception:
        pass
    return []


def _discover_compute_nodes() -> list[str]:
    try:
        r = subprocess.run(
            [SINFO_BIN, "-N", "-h", "-o", "%N"],
            capture_output=True, text=True, timeout=10
        )
        if r.returncode != 0:
            return []
        nodes = []
        for line in r.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            r2 = subprocess.run(
                ["/opt/slurm/bin/scontrol", "show", "hostnames", line],
                capture_output=True, text=True, timeout=5
            )
            nodes.extend([x.strip() for x in r2.stdout.splitlines() if x.strip()])
        nodes = sorted(set(nodes))
        nodes = [n for n in nodes if not re.search(r"(master|login|head|apps|ood|monitor)", n, re.I)]
        return nodes
    except Exception:
        return []


def _fetch_fs_metrics(node: str) -> dict:
    """Get /scratch and / filesystem metrics via Prometheus, fallback to SSH df."""
    try:
        res  = _prom_query(f'node_uname_info{{nodename="{node}"}}')
        inst = res[0]["metric"].get("instance") if res else f"{node}:9100"

        sizes, avails = {}, {}
        for r in _prom_query(f'node_filesystem_size_bytes{{instance="{inst}"}}'):
            mp = r["metric"].get("mountpoint", "")
            ft = r["metric"].get("fstype", "")
            if mp in IGNORE_MOUNTS or ft in IGNORE_FSTYPES or mp in NFS_MOUNTS:
                continue
            try:
                sizes[mp] = int(float(r["value"][1]))
            except (ValueError, IndexError):
                pass
        for r in _prom_query(f'node_filesystem_avail_bytes{{instance="{inst}"}}'):
            mp = r["metric"].get("mountpoint", "")
            if mp in sizes:
                try:
                    avails[mp] = int(float(r["value"][1]))
                except (ValueError, IndexError):
                    pass
        if sizes:
            result = {}
            for mp in sizes:
                sz   = sizes[mp]
                av   = avails.get(mp, 0)
                used = sz - av
                pct  = round((used / sz * 100), 1) if sz else 0
                result[mp] = {
                    "size": sz, "avail": av, "used": used, "pct": pct,
                    "size_h": _fmt_bytes(sz), "avail_h": _fmt_bytes(av), "used_h": _fmt_bytes(used)
                }
            return result
    except Exception:
        pass

    # Fallback: SSH df
    try:
        rc, out, _ = _ssh(node, "df -B1 /scratch / 2>/dev/null | tail -n +2", timeout=10)
        result = {}
        if rc == 0:
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 6:
                    mp = parts[5]
                    if mp in ["/scratch", "/"]:
                        sz   = int(parts[1])
                        av   = int(parts[3])
                        used = sz - av
                        pct  = round((used / sz * 100), 1) if sz else 0
                        result[mp] = {
                            "size": sz, "avail": av, "used": used, "pct": pct,
                            "size_h": _fmt_bytes(sz), "avail_h": _fmt_bytes(av), "used_h": _fmt_bytes(used)
                        }
        return result
    except Exception:
        return {}


def _fetch_active_scratch_paths(node: str) -> set[str]:
    """Get working directories of active jobs — for marking scratch entries as active."""
    try:
        r = subprocess.run(
            [SQUEUE_BIN, "-h", "-w", node, "-o", "%Z"],
            capture_output=True, text=True, timeout=10
        )
        if r.returncode == 0:
            return {p.strip() for p in r.stdout.splitlines() if p.strip()}
    except Exception:
        pass
    return set()


def _count_active_jobs(node: str) -> int:
    """Count running Slurm jobs on this node directly."""
    try:
        r = subprocess.run(
            [SQUEUE_BIN, "-h", "-w", node, "-o", "%i", "--states=RUNNING"],
            capture_output=True, text=True, timeout=10
        )
        if r.returncode == 0:
            return len([l for l in r.stdout.splitlines() if l.strip()])
    except Exception:
        pass
    return 0


def _scan_node(node: str) -> dict:
    """Scan a single node's /scratch via SSH."""
    stat_cmd = r"""
set -o pipefail
find /scratch -mindepth 1 -maxdepth 1 -print0 2>/dev/null | while IFS= read -r -d '' e; do
  n="$(basename -- "$e")"
  [[ "$n" == "lost+found" ]] && continue
  owner="$(stat -c '%U' -- "$e" 2>/dev/null || true)"
  [[ -z "$owner" ]] && owner="uid$(stat -c '%u' -- "$e" 2>/dev/null)"
  mtime="$(stat -c '%Y' -- "$e" 2>/dev/null || echo 0)"
  [[ -d "$e" ]] && kind="dir" || kind="file"
  printf '%s|%s|%s|%s\n' "$n" "$owner" "$mtime" "$kind"
done
""".strip()

    du_cmd = r"""
find /scratch -mindepth 1 -maxdepth 1 ! -name lost+found 2>/dev/null | while IFS= read -r e; do
  n="$(basename -- "$e")"
  kb="$(du -sk -- "$e" 2>/dev/null | cut -f1)"
  printf '%s\t%s\n' "${kb:-0}" "$n"
done
""".strip()

    try:
        rc1, stat_out, err1 = _ssh(node, stat_cmd, timeout=90)
        rc2, du_out, _      = _ssh(node, du_cmd,   timeout=120)
    except subprocess.TimeoutExpired:
        return {"node": node, "error": "SSH timeout", "entries": [], "fs": {}}
    except Exception as e:
        return {"node": node, "error": str(e)[:80], "entries": [], "fs": {}}

    if rc1 != 0 and not stat_out:
        return {"node": node, "error": f"stat failed: {err1[:120]}", "entries": [], "fs": {}}

    # Parse du sizes — format is "kb\tname"
    sizes: dict[str, int] = {}
    for line in du_out.splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            try:
                sizes[parts[1].strip()] = int(parts[0].strip())
            except ValueError:
                pass

    # Get active job paths
    active_paths = _fetch_active_scratch_paths(node)

    # Parse stat entries
    entries = []
    now = datetime.now()
    for line in stat_out.splitlines():
        parts = line.split("|", 3)
        if len(parts) < 4:
            continue
        name, owner, mtime_s, kind = parts
        if name in SKIP_NAMES:
            continue
        try:
            mtime = datetime.fromtimestamp(int(mtime_s))
        except (ValueError, OSError):
            mtime = now

        age_days = (now - mtime).days
        size_kb  = sizes.get(name, 0)
        full_path = f"/scratch/{name}"
        active_job = full_path in active_paths or any(
            full_path.startswith(p) for p in active_paths
        )

        if active_job:
            age_status = "active"
        elif age_days >= STALE_DAYS:
            age_status = "stale"
        elif age_days >= WARN_DAYS:
            age_status = "warn"
        else:
            age_status = "ok"

        if size_kb >= 100 * 1_048_576:
            size_status = "large"
        elif size_kb >= 10 * 1_048_576:
            size_status = "medium"
        else:
            size_status = "ok"

        entries.append({
            "name":        name,
            "path":        full_path,
            "owner":       owner,
            "size_kb":     size_kb,
            "size_h":      _fmt_bytes(size_kb * 1024),
            "mtime":       mtime.strftime("%Y-%m-%d %H:%M"),
            "age_days":    age_days,
            "kind":        kind,
            "age_status":  age_status,
            "size_status": size_status,
            "active_job":  active_job,
        })

    fs = _fetch_fs_metrics(node)

    # Summary stats
    total_kb  = sum(e["size_kb"] for e in entries)
    stale_n   = sum(1 for e in entries if e["age_status"] == "stale")
    stale_kb  = sum(e["size_kb"] for e in entries if e["age_status"] == "stale")
    active_n  = _count_active_jobs(node)

    scratch_fs = fs.get("/scratch", {})

    return {
        "node":      node,
        "error":     "",
        "entries":   entries,
        "fs":        fs,
        "summary": {
            "total_entries": len(entries),
            "total_size_h":  _fmt_bytes(total_kb * 1024),
            "stale_n":       stale_n,
            "stale_size_h":  _fmt_bytes(stale_kb * 1024),
            "active_n":      active_n,
            "scratch_pct":   scratch_fs.get("pct", 0),
            "scratch_used_h": scratch_fs.get("used_h", "—"),
            "scratch_free_h": scratch_fs.get("avail_h", "—"),
            "scratch_size_h": scratch_fs.get("size_h", "—"),
        }
    }


def _df_node(node: str) -> dict:
    """Get df summary for a storage/backup node — only real data pools under /mnt/tank"""
    try:
        rc, out, err = _ssh(node, "df -B1 2>/dev/null | tail -n +2", timeout=15)
        if rc != 0:
            return {"node": node, "online": False, "error": err[:100], "mounts": []}
        mounts = []
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 6:
                continue
            mp = parts[-1]
            # Normalize double /mnt/mnt/ prefix (storage-backup quirk)
            if mp.startswith("/mnt/mnt/"):
                mp = mp[4:]  # strip leading /mnt -> /mnt/tank1/...
            if not mp.startswith("/mnt/tank"):
                continue
            # Skip top-level pool mounts (e.g. /mnt/tank1, /mnt/tank2)
            pool_parts = mp.split("/")
            if len(pool_parts) <= 3:
                continue
            try:
                # df -B1 returns actual bytes on TrueNAS
                size_b  = int(parts[1])
                used_b  = int(parts[2])
                avail_b = int(parts[3])
                pct_int = int(parts[4].rstrip("%"))
            except (ValueError, IndexError):
                continue
            mounts.append({
                "mount":   mp,
                "size":    _fmt_bytes(size_b),
                "used":    _fmt_bytes(used_b),
                "avail":   _fmt_bytes(avail_b),
                "size_b":  size_b,
                "used_b":  used_b,
                "avail_b": avail_b,
                "pct":     pct_int,
                "fstype":  "zfs",
            })
        arc   = _fetch_arc_stats(node)
        snaps = _fetch_snapshots(node)
        # Fetch replication tasks for backup nodes
        repls = _fetch_replication_tasks(node) if node in ["storage-backup"] else []
        return {"node": node, "online": True, "error": "", "mounts": mounts,
                "arc": arc, "snapshots": snaps, "replications": repls}
    except Exception as e:
        return {"node": node, "online": False, "error": str(e)[:100], "mounts": [], "arc": {}, "snapshots": [], "replications": []}


def _fetch_arc_stats(host: str) -> dict:
    """Fetch ZFS ARC stats via arcstat."""
    try:
        rc, out, err = _ssh(host, "for p in arcstat /usr/bin/arcstat /usr/sbin/arcstat /usr/local/bin/arcstat; do sudo -n $p 1 1 2>/dev/null | tail -1 && break; done", timeout=15, login=True)
        if rc != 0 or not out:
            return {}
        # Parse arcstat output line: time read ddread ddh% dmread dmh% pread ph% size c avail
        parts = out.split()
        if len(parts) >= 9:
            def parse_size(s):
                s = s.strip()
                if not s or s == '-': return 0
                mults = {'K': 1024, 'M': 1048576, 'G': 1073741824, 'T': 1099511627776}
                if s[-1].upper() in mults:
                    try: return int(float(s[:-1]) * mults[s[-1].upper()])
                    except: return 0
                try: return int(s)
                except: return 0

            # arcstat columns: time read ddread ddh% dmread dmh% pread ph% size c avail
            # find size/c/avail by looking from end
            avail_h = parts[-1] if len(parts) > 10 else '—'
            max_h   = parts[-2] if len(parts) > 10 else '—'
            size_h  = parts[-3] if len(parts) > 10 else parts[8]
            ddh     = parts[3] if len(parts) > 3 else '0'
            dmh     = parts[5] if len(parts) > 5 else '0'
            return {
                "size":    parse_size(size_h),
                "size_h":  size_h,
                "max":     parse_size(max_h),
                "max_h":   max_h,
                "avail":   parse_size(avail_h),
                "avail_h": avail_h,
                "ddh_pct": ddh,
                "dmh_pct": dmh,
                "hit_pct": ddh,
            }
    except Exception:
        pass
    return {}


def _fetch_snapshots(host: str) -> list[dict]:
    """Fetch ZFS snapshots, excluding boot-pool. Use tab-separated output."""
    try:
        rc, out, err = _ssh(host,
            "sudo zfs list -t snapshot -H -p -o name,creation,used 2>/dev/null | grep -v boot-pool",
            timeout=15, login=True)
        if rc != 0 or not out:
            return []
        import datetime as _dt
        snaps = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            name = parts[0].strip()
            dataset, snap_name = name.rsplit("@", 1) if "@" in name else (name, "")
            try:
                ts = int(parts[1].strip())
                creation = _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
            except Exception:
                creation = parts[1].strip()
            try:
                used_b = int(parts[2].strip())
                used = _fmt_bytes(used_b)
            except Exception:
                used = parts[2].strip()
            snaps.append({
                "name":     name,
                "dataset":  dataset,
                "snapshot": snap_name,
                "creation": creation,
                "used":     used,
            })
        return snaps
    except Exception:
        return []


def _fetch_replication_tasks(host: str) -> list[dict]:
    """Fetch replication tasks via midclt — requires interactive TTY for sudo.
    Uses ssh -tt to allocate pseudo-TTY."""
    try:
        cmd = "for p in /usr/bin/midclt /usr/local/bin/midclt midclt; do sudo -n $p call replication.query '[]' 2>/dev/null && break; done"
        r = subprocess.run(
            ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=8",
             "-o", "BatchMode=yes", f"{SSH_USER}@{host}", "bash", "-l"],
            input=cmd, capture_output=True, text=True, timeout=20
        )
        # Find JSON array in output
        out = r.stdout
        # Try to find the JSON array
        start = -1
        for i, ch in enumerate(out):
            if ch == '[':
                start = i
                break
        if start == -1:
            return []
        json_str = out[start:]
        # Find matching closing bracket
        depth = 0
        end = -1
        for i, ch in enumerate(json_str):
            if ch == '[': depth += 1
            elif ch == ']':
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end == -1:
            return []
        json_str = json_str[:end]
        import json as _json
        tasks = _json.loads(json_str)
        result = []
        for t in tasks:
            state = t.get('state', {})
            job   = t.get('job') or {}
            sched = t.get('schedule') or {}
            # Format schedule
            hour = sched.get('hour', '*')
            minute = sched.get('minute', '0')
            schedule_str = f"Daily at {hour.zfill(2)}:{minute.zfill(2)}" if hour != '*' else 'Custom'
            # Format last run time
            finished = job.get('time_finished', {})
            if isinstance(finished, dict):
                ts = finished.get('$date', 0)
                import datetime
                last_run = datetime.datetime.fromtimestamp(ts/1000).strftime('%Y-%m-%d %H:%M') if ts else '—'
            else:
                last_run = '—'
            # Duration
            started = job.get('time_started', {})
            duration = '—'
            if isinstance(started, dict) and isinstance(finished, dict):
                s = started.get('$date', 0)
                f = finished.get('$date', 0)
                if s and f:
                    secs = (f - s) // 1000
                    duration = f"{secs//60}m {secs%60}s" if secs >= 60 else f"{secs}s"
            result.append({
                'name':          t.get('name', ''),
                'source':        ', '.join(t.get('source_datasets', [])),
                'target':        t.get('target_dataset', ''),
                'direction':     t.get('direction', 'PULL'),
                'enabled':       t.get('enabled', False),
                'state':         state.get('state', 'UNKNOWN'),
                'last_snapshot': state.get('last_snapshot', '—'),
                'last_run':      last_run,
                'duration':      duration,
                'schedule':      schedule_str,
                'retention':     f"{t.get('lifetime_value','?')} {t.get('lifetime_unit','').lower()}s",
                'progress':      job.get('progress', {}).get('description', ''),
            })
        return result
    except Exception as e:
        return []


# ── API endpoints ──────────────────────────────────────────────────────────

@router.get("/storage")
async def get_storage():
    results = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(_df_node, n): n for n in STORAGE_NODES}
        for f in as_completed(futures):
            results.append(f.result())
    results.sort(key=lambda x: x["node"])
    return {"nodes": results}


@router.get("/backups")
async def get_backups():
    results = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(_df_node, n): n for n in BACKUP_NODES}
        for f in as_completed(futures):
            results.append(f.result())
    results.sort(key=lambda x: x["node"])
    return {"nodes": results}


@router.get("/scratch/nodes")
async def get_scratch_nodes():
    """Return just the node list with summary — fast, no scanning."""
    nodes = _discover_compute_nodes()
    return {"nodes": [{"node": n, "scanned": False} for n in nodes]}


@router.get("/scratch/scan/{node}")
async def scan_node(node: str):
    """Scan a single node's /scratch."""
    # basic safety check
    if not re.match(r'^[\w\-]+$', node):
        raise HTTPException(status_code=400, detail="Invalid node name")
    return _scan_node(node)


@router.get("/scratch/scan-all")
async def scan_all():
    """Scan all compute nodes in parallel."""
    nodes = _discover_compute_nodes()
    if not nodes:
        raise HTTPException(status_code=500, detail="Could not discover compute nodes")
    results = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(_scan_node, n): n for n in nodes}
        for f in as_completed(futures):
            results.append(f.result())
    results.sort(key=lambda x: x["node"])
    return {"nodes": results}


class DeleteRequest(BaseModel):
    items: list[dict]  # [{node, path, name}]


@router.post("/scratch/delete")
async def delete_scratch(req: DeleteRequest):
    """Delete selected scratch entries."""
    results = []
    for item in req.items:
        node = item.get("node", "")
        path = item.get("path", "")
        # Safety checks
        if not re.match(r'^[\w\-]+$', node):
            results.append({"node": node, "path": path, "ok": False, "msg": "Invalid node"})
            continue
        if not path.startswith("/scratch/") or ".." in path:
            results.append({"node": node, "path": path, "ok": False, "msg": "Invalid path"})
            continue
        try:
            cmd = f"sudo rm -rf {path!r}"
            rc, _, err = _ssh(node, cmd, timeout=180)
            if rc == 0:
                results.append({"node": node, "path": path, "ok": True, "msg": f"Deleted {path}"})
            else:
                results.append({"node": node, "path": path, "ok": False, "msg": err[:120]})
        except subprocess.TimeoutExpired:
            results.append({"node": node, "path": path, "ok": False, "msg": "SSH timeout"})
        except Exception as e:
            results.append({"node": node, "path": path, "ok": False, "msg": str(e)[:120]})
    return {"results": results}
