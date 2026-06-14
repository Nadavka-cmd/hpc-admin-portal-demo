# GPU Policy: single source of truth for Slurm + Open OnDemand

A small mechanism that keeps GPU resource limits defined **once** and
propagated everywhere, so a hardware change is a one-line edit instead of a
hunt-and-replace across enforcement code, CLIs, and web UIs.

## The problem

On a shared GPU cluster the same per-partition limits tend to get duplicated:
in the Slurm `job_submit.lua` that enforces them, in any "what can I request?"
CLI, in a scheduler advisor, and in every Open OnDemand interactive app's form
(each app carries its own hardcoded legend/limits table). These copies drift —
a legend ends up advertising 64 GB while the scheduler enforces 120 GB, or a
walltime that no longer matches reality.

## The flow

```
gpu_policy.lua            <-- canonical, hand-edited (source of truth)
      |  gpu_policy_to_json.lua  (lua -> json serializer)
      v
gpu_policy.json           <-- generated; synced to every consumer
      |
      +--> job_submit.lua     (enforcement; reads the .lua directly)
      +--> gpulimits CLI      (reads the .json)
      +--> scheduler advisor  (reads the .json)
      +--> OOD app legends     (reads the .json, see ood/)
```

Edit `gpu_policy.lua`, regenerate the JSON, sync it. Everything downstream
follows automatically.

## Schema

Each partition entry, **per-GPU**, memory in MiB:

| field         | meaning                                              |
|---------------|------------------------------------------------------|
| `max_gpu`     | hard ceiling on GPUs per job                         |
| `rec_cpu`     | recommended CPUs (starting point)                    |
| `max_cpu`     | hard CPU ceiling **per GPU**                         |
| `rec_mem`     | recommended memory, MiB                              |
| `max_mem`     | hard memory ceiling **per GPU**, MiB                 |
| `gpu_model`   | display-only hardware name (legend)                  |
| `vram`        | display-only VRAM string (legend)                    |
| `restricted`  | `true` hides the partition from the general pool     |
| `owner_group` | AD group allowed to see a restricted partition       |

Limits are per-GPU on purpose. Per-job totals (and access control) are expected
to come from Slurm **QoS**, not from this table — this is the resource catalog,
not the enforcement engine.

## Regenerate + sync (runbook)

```bash
# 1. edit the canonical table
sudo nano /etc/slurm/gpu_policy.lua

# 2. regenerate the JSON (paths overridable via GPU_POLICY_SRC / GPU_POLICY_DST)
sudo lua gpu_policy_to_json.lua

# 3. distribute gpu_policy.json to consumers (your config-sync mechanism)
```

That's the whole change surface for a hardware swap.

## Open OnDemand integration (`ood/`)

OOD only runs server-side code in `form.yml.erb`; `form.js` is a static asset,
so there is no native ERB -> JS data channel. The bridge:

1. **`legend_block.rb`** — a Ruby fragment injected into the form's ERB header.
   It reads `gpu_policy.json`, layers on presentation (colors/notes) and
   *interactive* overlays, and produces `legend_b64`.
2. **the carrier** — `legend_b64` is embedded in the partition field's `help:`
   as `<span id="gpu-policy-legend" style="display:none"><%= legend_b64 %></span>`.
   OOD renders `help` as HTML, so the span lands in the DOM. base64 is used so
   the payload survives any HTML escaping (no quotes/angle-brackets).
3. **`legend_loader.js`** — replaces the app's hardcoded `partitionLegend`
   object. Reads the span, base64-decodes **UTF-8-safe via `TextDecoder`**
   (plain `atob()` mangles multi-byte chars like en-dashes in labels), and
   falls back to `{}` so a missing blob degrades gracefully.

### Overlays vs canonical

Some legend values are *interactive-app policy*, not hardware facts, so they
live in `legend_block.rb`, not in `gpu_policy.lua`:

- **GPU cap** — interactive sessions are capped (default 2). The legend shows
  `min(max_gpu, cap)`; a locked teaching partition is pinned to 1.
- **Walltime** — the interactive session ceiling (e.g. 8 h; 4 h for the locked
  partition), which is deliberately *not* the partition's Slurm `MaxTime`.
- **CPU-only partition** — has no GPU and isn't in the canonical table, so it's
  carried as a small constant; its GPU sub-header and "Max GPUs" row are
  suppressed.
- **`/GPU` suffix** — shown on CPU/RAM rows only when more than one GPU is
  selectable (derived, not stored).

## Applying it to an app

```bash
# convert an app from a hardcoded legend to the canonical-driven one
sudo python3 ood/patch_legend.py /var/www/ood/apps/sys/<app>

# enforce the interactive GPU cap so the field matches the legend
sudo python3 ood/patch_caps.py   /var/www/ood/apps/sys/<app>

# add an app-specific legend row (e.g. a fixed container on a locked partition)
sudo python3 ood/patch_caps.py   /var/www/ood/apps/sys/<app> \
     --extra-row 'Container|PyTorch / CUDA 12.1 (fixed)|course'
```

Both patchers are idempotent, back up every file they touch, and exit without
writing if an anchor doesn't match (they never half-apply). The anchor strings
at the top of `patch_legend.py` (the partition `help:` text, the ERB header
boundary) may need adjusting to match your forms.

## Gotchas

- **UTF-8** — always decode with `TextDecoder`, never bare `atob()`.
- **`help` must render as HTML** — true for stock OOD; if a theme escapes it,
  switch the carrier to a hidden form field.
- **The legend shows the interactive cap**, not the partition's QoS maximum.
  Keep the field cap (`patch_caps.py`) in step with the legend or it will lie
  again — which is the whole thing this mechanism exists to prevent.

## Layout

```
gpu_policy.lua            canonical table (example values)
gpu_policy_to_json.lua    lua -> json serializer
ood/
  legend_block.rb         ERB fragment (server-side: json -> legend_b64)
  legend_loader.js        form.js loader (browser-side: blob -> partitionLegend)
  patch_legend.py         convert an app to the canonical-driven legend
  patch_caps.py           enforce GPU cap + optional app-specific row
```
