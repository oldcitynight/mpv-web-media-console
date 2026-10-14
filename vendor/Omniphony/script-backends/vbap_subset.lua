-- Engine-helper demo: pan using only a SUBSET of the layout's speakers.
--
-- Build a VBAP panner over the chosen subset once in `setup` (vbap_new), then in
-- `gains` compute the subset gains and scatter them back into full speaker order.
-- Here we keep the ground ring (|z| small) and any height speakers (z > 0.5) and
-- drop everything else; a `floor_only` toggle restricts to the ground ring.
--
-- Shows: vbap_new(subset) -> object with :gains(pos)/:count(); index mapping.

function params()
  return {
    {
      key = "floor_only", label = "Floor only", min = 0, max = 1, step = 1,
      default = 0, help = "1 = use only the ground ring; 0 = ground ring + tops.",
    },
  }
end

function setup(speakers, params)
  local floor_only = (params.floor_only or 0) >= 0.5
  local subset, map = {}, {}
  for i, s in ipairs(speakers) do
    local is_floor = math.abs(s.z) < 0.1
    local is_top = s.z > 0.5
    if is_floor or (is_top and not floor_only) then
      subset[#subset + 1] = s
      map[#map + 1] = i
    end
  end
  -- vbap_new needs at least 3 non-degenerate directions; let it error clearly
  -- (surfaced in Studio) if the subset is too small or coplanar.
  return { v = vbap_new(subset), map = map, n = #speakers }
end

function gains(pos, speakers, state, params)
  local sub = state.v:gains(pos)
  local out = {}
  for i = 1, state.n do out[i] = 0.0 end
  for k, g in ipairs(sub) do
    out[state.map[k]] = g
  end
  return out
end
