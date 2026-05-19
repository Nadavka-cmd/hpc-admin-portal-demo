import yaml
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config.yaml"

with open(CONFIG_PATH) as f:
    _cfg = yaml.safe_load(f)

portal = _cfg.get("portal", {})
grafana = _cfg.get("grafana", {})
awx = _cfg.get("awx", {})
slurm = _cfg.get("slurm", {})
