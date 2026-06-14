#!/usr/bin/env lua
-- gpu_policy_to_json.lua
-- Reads the canonical gpu_policy.lua table and writes gpu_policy.json for
-- downstream consumers. Hand-rolled serializer, no external JSON library.
--
-- Paths are overridable via environment:
--   GPU_POLICY_SRC  (default /etc/slurm/gpu_policy.lua)
--   GPU_POLICY_DST  (default /etc/slurm/gpu_policy.json)
--
-- Run after every edit to gpu_policy.lua, then sync the JSON to consumers:
--   sudo lua gpu_policy_to_json.lua

local SRC = os.getenv("GPU_POLICY_SRC") or "/etc/slurm/gpu_policy.lua"
local DST = os.getenv("GPU_POLICY_DST") or "/etc/slurm/gpu_policy.json"

local policy = dofile(SRC)

local INT_FIELDS  = { "max_gpu", "rec_cpu", "max_cpu", "rec_mem", "max_mem" }
local BOOL_FIELDS = { "restricted" }
local STR_FIELDS  = { "owner_group", "gpu_model", "vram" }

local function esc(s)
  s = tostring(s)
  s = s:gsub('\\', '\\\\'):gsub('"', '\\"')
  return s
end

local parts = {}
for name in pairs(policy) do parts[#parts + 1] = name end
table.sort(parts)

local out = { "{" }
for pi, pname in ipairs(parts) do
  local entry = policy[pname]
  local fields = {}
  for _, f in ipairs(INT_FIELDS) do
    if entry[f] ~= nil then fields[#fields + 1] = string.format('    "%s": %d', f, entry[f]) end
  end
  for _, f in ipairs(BOOL_FIELDS) do
    if entry[f] ~= nil then fields[#fields + 1] = string.format('    "%s": %s', f, tostring(entry[f])) end
  end
  for _, f in ipairs(STR_FIELDS) do
    if entry[f] ~= nil then fields[#fields + 1] = string.format('    "%s": "%s"', f, esc(entry[f])) end
  end
  local comma = (pi < #parts) and "," or ""
  out[#out + 1] = string.format('  "%s": {\n%s\n  }%s', pname, table.concat(fields, ",\n"), comma)
end
out[#out + 1] = "}"

local fh, err = io.open(DST, "w")
if not fh then
  io.stderr:write("ERROR: cannot open " .. DST .. " for writing: " .. tostring(err) .. "\n")
  os.exit(1)
end
fh:write(table.concat(out, "\n") .. "\n")
fh:close()
io.write("Wrote " .. DST .. " (" .. #parts .. " partitions)\n")
