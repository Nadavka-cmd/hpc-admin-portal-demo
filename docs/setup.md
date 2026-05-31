# HPC Admin Portal — Setup Guide

A web-based administration portal for Slurm HPC clusters. Built with FastAPI + Jinja2, served behind a reverse proxy (e.g. Open OnDemand), and designed to run on the Slurm controller node.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Prerequisites](#2-prerequisites)
3. [Installation](#3-installation)
4. [Configuration — config.yaml](#4-configuration--configyaml)
5. [Sudo Rules](#5-sudo-rules)
6. [SSH Key Setup](#6-ssh-key-setup)
7. [Systemd Service](#7-systemd-service)
8. [Reverse Proxy Setup](#8-reverse-proxy-setup)
9. [Feature Overview](#9-feature-overview)
10. [Optional Integrations](#10-optional-integrations)

---

## 1. Architecture Overview

```
Browser → Reverse Proxy (OOD / nginx) → uvicorn (FastAPI) on port 8765
                                              │
                              ┌───────────────┼───────────────┐
                              ▼               ▼               ▼
                         Slurm binaries   LDAP/AD        SSH to nodes
                         (sinfo/squeue    (ldap3)        (config sync,
                          sacctmgr etc)                   ZFS quotas,
                                                          storage)
```

The portal runs on the **Slurm controller node** as a low-privilege service account. It calls Slurm binaries directly (no API), connects to LDAP/AD for user/group management, and SSHes to compute/storage nodes for config sync and quota management.

---

## 2. Prerequisites

### On the controller node
- Python 3.9+
- Slurm binaries accessible (e.g. `/opt/slurm/bin/`)
- `sacctmgr`, `scontrol`, `sinfo`, `squeue`, `sacct` in PATH or configured path
- `msmtp` (for mail features) — optional
- `ldap-utils` (`ldapsearch`) for AD/LDAP queries

### Cluster requirements
- Slurm with `AccountingStorageType=accounting_storage/slurmdbd`
- `AccountingStorageEnforce=associations,limits,qos` recommended
- SSH key access from controller to all compute nodes (passwordless, for config sync)

### Optional integrations
- **AWX / Ansible Tower** — for automation tab
- **TrueNAS / ZFS storage nodes** — for quota management
- **Prometheus** — for storage metrics
- **Open OnDemand** — for reverse proxy / SSO

---

## 3. Installation

```bash
# Clone the demo repo
git clone https://github.com/Nadavka-cmd/hpc-admin-portal-demo.git /opt/hpc-portal
cd /opt/hpc-portal

# Create virtualenv
python3.9 -m venv venv
source venv/bin/activate

# Install dependencies
pip install fastapi uvicorn[standard] jinja2 pydantic python-dotenv pyyaml httpx ldap3

# Create config file from example
cp config.yaml.example config.yaml
# Edit config.yaml with your values — see Section 4
```

---

## 4. Configuration — config.yaml

Create `/opt/hpc-portal/config.yaml`. This file contains all secrets and site-specific values. **Never commit this file to git.**

```yaml
portal:
  secret_key: "change-me-to-a-random-string"   # used for session signing
  admin_users:                                   # AD usernames allowed to log in
    - admin1
    - admin2

slurm:
  bin_path: /opt/slurm/bin                       # path to sinfo, squeue, sacctmgr etc.

ssh:
  user: svcaccount                               # service account used for SSH to nodes

ldap:
  uri: ldap://ldap.example.com                   # your LDAP/AD server
  base: DC=ldap,DC=example,DC=com               # base DN for searches
  hpc_ou: "OU=HPC,OU=IT,OU=Departments,DC=ldap,DC=example,DC=com"
  domain: example.com                            # AD domain for bind user UPN

mail:
  msmtp: /usr/bin/msmtp                          # path to msmtp binary
  from_addr: hpc-admin@example.com               # sender address for bulk mail
  # mail groups are resolved from LDAP — no extra config needed

grafana:
  url: http://monitor.example.com:3000           # Grafana base URL (optional)
  token: ""                                      # Grafana API token (optional)

awx:
  url: http://awx.example.com:30457             # AWX / Ansible Tower URL (optional)
  token: ""                                      # AWX API token (optional)

storage:
  prometheus: http://monitor.example.com:9090   # Prometheus URL for storage metrics
  nodes:                                         # storage/NFS nodes to monitor
    - storage1
    - storage2
    - storage3
  backup_nodes:
    - storage1

zfs:
  ssh_user: svcaccount
  datasets:
    - label: "home (storage2)"
      host: storage2
      dataset: tank2/home
      idx: 0
    - label: "projects (storage3)"
      host: storage3
      dataset: tank3/projects
      idx: 1

onboarding:
  home_base: /storage/home                       # base path for new user home dirs
```

---

## 5. Sudo Rules

The service account needs passwordless sudo for specific commands on the **controller node**. Add to `/etc/sudoers.d/hpc-portal`:

```
# Allow portal service account to read/write Slurm and system config files
svcaccount ALL=(root) NOPASSWD: /usr/bin/tee /etc/slurm/slurm.conf
svcaccount ALL=(root) NOPASSWD: /usr/bin/cat /etc/slurm/slurm.conf
svcaccount ALL=(root) NOPASSWD: /usr/bin/cat /etc/sssd/sssd.conf
svcaccount ALL=(root) NOPASSWD: /usr/bin/tee /etc/hosts
svcaccount ALL=(root) NOPASSWD: /usr/bin/cat /etc/hosts
svcaccount ALL=(root) NOPASSWD: /usr/bin/md5sum /etc/slurm/slurm.conf
svcaccount ALL=(root) NOPASSWD: /usr/bin/md5sum /etc/sssd/sssd.conf
svcaccount ALL=(root) NOPASSWD: /usr/bin/cp /etc/slurm/slurm.conf *
svcaccount ALL=(root) NOPASSWD: /usr/bin/cp /etc/sssd/sssd.conf *
svcaccount ALL=(root) NOPASSWD: /opt/slurm/bin/sacct *
```

On **compute nodes** (for config sync), the same service account needs:

```
svcaccount ALL=(root) NOPASSWD: /usr/bin/mv /tmp/.hpc_sync_* *
svcaccount ALL=(root) NOPASSWD: /usr/bin/chown * /etc/slurm/slurm.conf
svcaccount ALL=(root) NOPASSWD: /usr/bin/chmod * /etc/slurm/slurm.conf
svcaccount ALL=(root) NOPASSWD: /usr/bin/md5sum /etc/slurm/slurm.conf
svcaccount ALL=(root) NOPASSWD: /usr/bin/md5sum /etc/sssd/sssd.conf
svcaccount ALL=(root) NOPASSWD: /opt/slurm/bin/scontrol reconfigure
svcaccount ALL=(root) NOPASSWD: /usr/bin/systemctl restart sssd
```

On **ZFS/storage nodes**:

```
svcaccount ALL=(root) NOPASSWD: /sbin/zfs userspace *
svcaccount ALL=(root) NOPASSWD: /sbin/zfs set userquota@* *
```

---

## 6. SSH Key Setup

The service account must be able to SSH passwordlessly from the controller to all compute and storage nodes:

```bash
# On the controller, as the service account
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N ""

# Copy to each compute node
ssh-copy-id svcaccount@node01
ssh-copy-id svcaccount@node02
# ... repeat for all nodes and storage nodes

# Test
ssh -o BatchMode=yes svcaccount@node01 echo ok
```

---

## 7. Systemd Service

Create `/etc/systemd/system/hpc-portal.service`:

```ini
[Unit]
Description=HPC Admin Portal
After=network.target

[Service]
Type=simple
User=svcaccount
WorkingDirectory=/opt/hpc-portal
ExecStart=/opt/hpc-portal/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8765
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable hpc-portal
sudo systemctl start hpc-portal
sudo systemctl status hpc-portal
```

---

## 8. Reverse Proxy Setup

### Option A — Open OnDemand (OOD)

Create `/etc/ood/config/apps/sys/hpc-portal.conf` or add a portal app entry pointing to `http://127.0.0.1:8765`. The portal is designed to run at the subpath `/hpc-portal/` — the `root_path` is set in `main.py` accordingly.

### Option B — nginx

```nginx
location /hpc-portal/ {
    proxy_pass http://127.0.0.1:8765/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

---

## 9. Feature Overview

| Tab | Feature | Backend |
|-----|---------|---------|
| **Slurm Admin → Nodes** | Node state, drain/resume/down actions | `sinfo`, `scontrol` |
| **Slurm Admin → Jobs** | Live job queue, cancel/hold/requeue | `squeue`, `sacct` |
| **Slurm Admin → Config Sync** | Push config files to all nodes, diff view, inline editors | SSH + `sudo tee` |
| **Slurm Admin → Accounts** | Slurm account/user/QoS associations | `sacctmgr` |
| **Slurm Admin → AD Groups** | AD group membership management | `ldapsearch` / LDAP |
| **Slurm Admin → QoS Policy** | Full QoS CRUD, partition AllowQOS + DenyAccounts editor, swimlane map | `sacctmgr`, `scontrol` |
| **Slurm Admin → Send Mail** | Bulk email to AD groups | `msmtp` + LDAP |
| **Storage** | Filesystem usage, backups, scratch, ZFS quotas | SSH + `zfs`, Prometheus |
| **Automation** | AWX job templates, inventory, onboarding wizard | AWX REST API |

---

## 10. Optional Integrations

### AWX / Ansible Tower
Set `awx.url` and `awx.token` in `config.yaml`. The Automation tab will show your job templates and allow launching them from the portal. Without AWX configured, the Automation tab is non-functional.

### Prometheus
Set `storage.prometheus` in `config.yaml` for filesystem metrics on the Storage tab. Falls back to SSH `df` if Prometheus is unavailable.

### msmtp
Install and configure `msmtp` on the controller node for the Send Mail feature. Set `mail.msmtp` to the binary path and `mail.from_addr` to the sender address. Mail recipients are resolved from AD group membership via LDAP.

### ZFS Quotas
Configure `zfs.datasets` in `config.yaml` with your storage node hostnames and ZFS dataset paths. The service account must have passwordless SSH and sudo access to `zfs userspace` and `zfs set userquota@` on those nodes.
