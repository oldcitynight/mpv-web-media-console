-- Engine-helper demo: pan with the engine's VBAP, controlled by a single spread
-- slider, then normalise to constant power. The whole panning law is the engine's
-- VBAP — this script just exposes its `spread` and energy-normalises the result.
--
-- It shows the two engine-provided globals:
--   vbap(pos [, spread])   -> per-speaker VBAP gains (speaker order)
--   normalize_energy(out)  -> the array scaled to unit energy (constant power)

function params()
  return {
    {
      key = "spread", label = "Spread", min = 0.0, max = 1.0, step = 0.01,
      default = 0.2, help = "VBAP source spread: 0 = a point source, 1 = wide.",
    },
  }
end

function gains(pos, speakers, state, params)
  local spread = params.spread or 0.0
  -- Engine VBAP gains for this position, then constant-power normalisation.
  return normalize_energy(vbap(pos, spread))
end
