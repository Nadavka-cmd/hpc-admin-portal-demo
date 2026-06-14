-- gpu_policy.lua
-- Canonical per-partition GPU resource policy: the single source of truth.
--
-- Loaded directly by job_submit.lua for enforcement, and converted to
-- gpu_policy.json (see gpu_policy_to_json.lua) for downstream read-only
-- consumers: a `gpulimits` CLI, a scheduler advisor, and the Open OnDemand
-- interactive-app legends.
--
-- Field semantics (all limits are PER-GPU; memory in MiB):
--   rec_cpu / rec_mem : recommended starting point
--   max_cpu / max_mem : hard ceiling
--   max_gpu           : hard ceiling on GPUs per job
--   gpu_model / vram  : display-only hardware facts (shown in OOD legends)
--   restricted        : true hides the partition from the general population
--   owner_group       : AD group (case-insensitive) allowed to see a
--                       restricted partition; matches Slurm AllowGroups
--
-- EXAMPLE values. Replace partition names, limits, and groups with your own.
-- Per-job (vs per-GPU) enforcement is expected from Slurm QoS, not this table.

return {
  course          = { max_gpu=2, rec_cpu=8,  max_cpu=8,  rec_mem=29500, max_mem=29500,  gpu_model="GTX 1080 Ti", vram="11 GB",    restricted=false, owner_group="" },
  shared_a5000    = { max_gpu=4, rec_cpu=8,  max_cpu=8,  rec_mem=56000, max_mem=60000,  gpu_model="RTX A5000",   vram="24 GB",    restricted=false, owner_group="" },
  shared_a6000    = { max_gpu=4, rec_cpu=48, max_cpu=48, rec_mem=65536, max_mem=120000, gpu_model="RTX A6000",   vram="48 GB",    restricted=false, owner_group="" },
  shared          = { max_gpu=4, rec_cpu=8,  max_cpu=8,  rec_mem=26000, max_mem=30000,  gpu_model="Mixed",       vram="11-48 GB", restricted=false, owner_group="" },
--- Researcher-owned (restricted) ---
  research_groupa = { max_gpu=4, rec_cpu=8,  max_cpu=8,  rec_mem=45000, max_mem=50000,  gpu_model="RTX A5000",   vram="24 GB",    restricted=true,  owner_group="hpc_groupa" },
}
