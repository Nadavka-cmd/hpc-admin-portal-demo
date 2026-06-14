#!/usr/bin/env python3
"""
patch_legend.py -- convert an Open OnDemand interactive app from a hardcoded
partition legend to one driven by the canonical gpu_policy.json.

It:
  1. injects legend_block.rb into the form.yml.erb header block,
  2. appends the base64 carrier <span> to the partition field's help line,
  3. replaces the hardcoded `const partitionLegend = {...}` in form.js with
     the loader from legend_loader.js,
  4. makes the legend's gpu_model/VRAM sub-header conditional (so CPU-only
     partitions with empty gpu_model render cleanly).

Validates every required anchor BEFORE writing anything (never half-applies),
is idempotent, and backs up each file (.bak.<date>). Adjust the ANCHOR
constants below if your form files differ.

Usage:
  sudo python3 patch_legend.py /var/www/ood/apps/sys/<app> [--snippets DIR]
"""
import re, sys, os, shutil, argparse, datetime

# ---- anchors: adjust to your form files if they differ ----
HELP_OLD   = 'help: "Only partitions you are authorized to use are shown"'
ERB_ANCHOR = "\n%>\n---"        # end of the ERB header block, before the YAML doc
CARRIER_ID = "gpu-policy-legend"
LEGEND_RE  = re.compile(r"const partitionLegend = \{.*?\};\n?", re.S)  # single- or multi-line
SUBHDR_RE  = re.compile(
    r'(<div style="color:#666;margin-bottom:6px">\$\{info\.gpu_model\}.*?VRAM</div>)', re.S)

ap = argparse.ArgumentParser()
ap.add_argument("app", help="OOD app dir containing form.yml.erb and form.js")
ap.add_argument("--snippets", default=os.path.dirname(os.path.abspath(__file__)),
                help="dir with legend_block.rb and legend_loader.js")
args = ap.parse_args()

ERB = os.path.join(args.app, "form.yml.erb")
JS  = os.path.join(args.app, "form.js")
stamp = datetime.date.today().isoformat()

ruby   = open(os.path.join(args.snippets, "legend_block.rb"),  encoding="utf-8").read().rstrip("\n") + "\n"
loader = open(os.path.join(args.snippets, "legend_loader.js"), encoding="utf-8").read()
help_new = ('help: \'Only partitions you are authorized to use are shown'
            '<span id="%s" style="display:none"><%%= legend_b64 %%></span>\'' % CARRIER_ID)

erb = open(ERB, encoding="utf-8").read()
js  = open(JS,  encoding="utf-8").read()
new_erb, new_js = erb, js

# ---- compute + validate ERB (no write yet) ----
if "legend_b64" in erb:
    print("ERB already patched, skipping.")
else:
    if ERB_ANCHOR not in erb: sys.exit("ERROR: ERB header-block anchor not found")
    if HELP_OLD not in erb:   sys.exit("ERROR: partition help line not found (adjust HELP_OLD)")
    new_erb = erb.replace(ERB_ANCHOR, "\n" + ruby + "%>\n---", 1).replace(HELP_OLD, help_new, 1)

# ---- compute + validate JS (no write yet) ----
if CARRIER_ID in js:
    print("JS loader already patched, skipping.")
else:
    new_js, n = LEGEND_RE.subn(loader, js, count=1)
    if n != 1: sys.exit("ERROR: 'const partitionLegend = {...};' block not found in form.js")

if "info.gpu_model ?" not in new_js:
    new_js2, n2 = SUBHDR_RE.subn(r'${info.gpu_model ? `\1` : ""}', new_js, count=1)
    if n2 == 1: new_js = new_js2
    else: print("WARN: sub-header line not found (skipped) -- check renderLegend markup.")

# ---- all validations passed; write ----
if new_erb != erb:
    shutil.copy(ERB, f"{ERB}.bak.{stamp}"); open(ERB, "w", encoding="utf-8").write(new_erb)
    print(f"Patched {ERB} (backup .bak.{stamp})")
if new_js != js:
    shutil.copy(JS, f"{JS}.bak.{stamp}"); open(JS, "w", encoding="utf-8").write(new_js)
    print(f"Patched {JS} (backup .bak.{stamp})")
print("Done.")
