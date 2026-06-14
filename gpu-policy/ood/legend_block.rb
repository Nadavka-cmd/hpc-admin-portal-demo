# legend_block.rb
# Ruby fragment injected into an OOD app's form.yml.erb header block (the
# existing <% ... %> that already defines `partition_labels` and reads Slurm
# partitions). It reads gpu_policy.json, layers on presentation + interactive
# overlays, and produces `legend_b64` -- a base64 blob the page hands to
# legend_loader.js.
#
# After injecting this, embed the blob in the partition field's `help:` (OOD
# renders help as HTML), so the browser can read it:
#   help: 'Only partitions you can use are shown<span id="gpu-policy-legend"
#          style="display:none"><%= legend_b64 %></span>'

require 'json'
require 'base64'
_policy_json = ENV['GPU_POLICY_JSON'] || '/etc/slurm/gpu_policy.json'
_gp = (JSON.parse(File.read(_policy_json)) rescue {})

# Presentation only (color, border, note) -- NOT hardware facts. Keyed by
# partition name; unknown partitions fall back to a neutral grey.
_pres = {
  "course"          => ["#cce5ff", "#99caff", "All settings locked for fair use. One job at a time."],
  "shared_a5000"    => ["#cfeeea", "#97d8cf", nil],
  "shared_a6000"    => ["#f8d7da", "#f1aeb5", "High VRAM node. Request accordingly."],
  "shared"          => ["#dde3ff", "#b3c0f0", "Scheduler picks from all GPU nodes."],
  "research_groupa" => ["#e2d6f3", "#c5a8e8", nil],
  "CPUonly"         => ["#ece3d2", "#d6c6a4", nil],
}

# App-policy overlays -- interactive limits, NOT canonical hardware facts.
_cpu = {"max_gpu"=>0, "max_cpu"=>10, "max_mem"=>60000, "gpu_model"=>"", "vram"=>""}  # CPU-only: not in canonical
_cap = 2                                         # interactive GPU cap per session
_wt  = Hash.new("8 h").merge("course" => "4 h")  # interactive walltime per partition
_gb  = lambda { |m| g = m.to_f / 1000.0; (g % 1 == 0) ? "#{g.to_i} GB" : "#{g} GB" }

_legend = {}
(_gp.keys + ["CPUonly"]).uniq.each do |pp|
  s = (pp == "CPUonly") ? _cpu : _gp[pp]
  next unless s
  shown = (pp == "course") ? 1 : [s["max_gpu"].to_i, _cap].min   # course locked to 1
  pr = _pres.fetch(pp, ["#e9ecef", "#ced4da", nil])
  rows = []
  rows << {"k"=>"Max GPUs", "v"=>shown.to_s} if shown > 0        # omit for CPU-only
  rows << {"k"=>"Max CPUs", "v"=>s["max_cpu"].to_s}
  rows << {"k"=>"Max RAM",  "v"=>_gb.call(s["max_mem"])}
  rows << {"k"=>"Walltime", "v"=>_wt[pp]}
  _legend[pp] = {
    "label"     => (partition_labels[pp] || pp),
    "color"     => pr[0], "border" => pr[1],
    "gpu_model" => s["gpu_model"], "gpu_mem" => s["vram"],
    "multi_gpu" => (shown > 1),     # per-GPU "/GPU" suffix only when >1 shown
    "rows"      => rows,
    "note"      => pr[2],
  }
end
legend_b64 = Base64.strict_encode64(_legend.to_json)
