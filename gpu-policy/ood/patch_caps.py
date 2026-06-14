#!/usr/bin/env python3
"""
patch_caps.py -- enforce the interactive GPU cap in an OOD app's form so the
field can't exceed what the legend advertises, and (optionally) add an
app-specific legend row.

  1. sets the gpus field `max:` in form.yml.erb to GPU_CAP,
  2. clamps it in applyLimits() so the per-partition table can't re-raise it,
  3. if --extra-row "<KEY>|<VALUE>|<PARTITION>" is given, inserts an extra
     legend row for that partition (e.g. a fixed container image on a locked
     course partition). Requires patch_legend.py to have run first.

Idempotent; backs up changed files (.precap.<date>). The GPU cap is written
even if an optional --extra-row anchor is missing (that only warns).

Usage:
  sudo python3 patch_caps.py /var/www/ood/apps/sys/<app>
  sudo python3 patch_caps.py /var/www/ood/apps/sys/jupyter \
       --extra-row 'Container|PyTorch / CUDA 12.1 (fixed)|course'
"""
import re, os, sys, shutil, argparse, datetime

GPU_CAP = 2  # interactive sessions: max GPUs per session

ap = argparse.ArgumentParser()
ap.add_argument("app")
ap.add_argument("--extra-row", help='KEY|VALUE|PARTITION legend row to add')
args = ap.parse_args()

ERB = os.path.join(args.app, "form.yml.erb")
JS  = os.path.join(args.app, "form.js")
stamp = datetime.date.today().isoformat()

erb = open(ERB, encoding="utf-8").read()
js  = open(JS,  encoding="utf-8").read()
erb0, js0 = erb, js

# ---- 1. cap the gpus field at GPU_CAP ----
if "_gcap" in js:
    print("GPU cap already applied, skipping.")
else:
    erb, n = re.subn(r'(  gpus:.*?\n    max: )\d+', r'\g<1>%d' % GPU_CAP, erb, count=1, flags=re.S)
    if n != 1: sys.exit("ERROR: gpus 'max:' not found in ERB")
    old = ('    gpus.max = lim.max_gpus;\n'
           '    if (parseInt(gpus.value) > lim.max_gpus) gpus.value = lim.max_gpus;')
    new = ('    var _gcap = Math.min(lim.max_gpus, %d);\n'
           '    gpus.max = _gcap;\n'
           '    if (parseInt(gpus.value) > _gcap) gpus.value = _gcap;') % GPU_CAP
    if old not in js: sys.exit("ERROR: gpus block not found in applyLimits")
    js = js.replace(old, new, 1)
    print(f"GPU cap -> {GPU_CAP} applied.")

# ---- 2. optional app-specific legend row (non-fatal if anchor missing) ----
if args.extra_row:
    k, v, part = args.extra_row.split("|", 2)
    if f'"{k}"' in erb:
        print(f'"{k}" row already present, skipping.')
    else:
        m = re.search(r'rows << \{"k"=>"Walltime",\s*"v"=>_wt\[pp\]\}', erb)
        if not m:
            print('WARN: Walltime row anchor not found -- run patch_legend.py first; extra row skipped.')
        else:
            row = '\n  rows << {"k"=>"%s", "v"=>"%s"} if pp == "%s"' % (k, v, part)
            erb = erb[:m.end()] + row + erb[m.end():]
            print(f'Inserted "{k}" row for partition "{part}".')

if erb != erb0: shutil.copy(ERB, f"{ERB}.precap.{stamp}"); open(ERB, "w", encoding="utf-8").write(erb)
if js  != js0:  shutil.copy(JS,  f"{JS}.precap.{stamp}");  open(JS,  "w", encoding="utf-8").write(js)
print("Done." if (erb != erb0 or js != js0) else "No changes. Done.")
