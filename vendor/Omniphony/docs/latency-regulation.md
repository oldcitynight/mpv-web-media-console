# Latency Regulation Algorithm

This document describes the current realtime output latency regulation used by `omniphony-renderer`, with emphasis on the shared control model and the backend-specific differences between `ASIO` and `PipeWire`.

## Goals

The latency controller has four jobs:

1. Keep the audible output close to a configured target latency.
2. Recover from low-buffer and high-buffer excursions without letting unstable audio leak through.
3. Support adaptive local resampling when enabled.
4. Report enough state to the UI so recovery behavior is observable.

The long-term control target is not "minimum latency". It is "stable latency near a requested setpoint, with predictable recovery behavior".

## Core Model

The shared regulation logic lives in [adaptive_runtime.rs](/home/user/dev/spatial-renderer/Omniphony/omniphony-renderer/audio_output/src/adaptive_runtime.rs).

### Domains

Two sample domains matter:

- Input domain: decoded/rendered samples written into the backend ring buffer.
- Output domain: samples actually consumed by the backend callback after local resampling.

Latency control is intentionally expressed in the input domain so the controller reasons about the same audio inventory regardless of output sample rate.

### Measured Quantities

For each callback, the backend computes:

- `available_input_samples`: current ring-buffer fill.
- `output_fifo_input_domain_samples`: local resampler FIFO converted back to input-domain samples.
- `callback_input_domain_samples`: callback size converted to input domain.
- `control_available`: `ring + output_fifo - callback/2`.
- `control_latency_ms`: `control_available / (sample_rate * channels)`.
- `measured_latency_ms`: `control_latency_ms + graph/backend latency estimate`.

`control_latency_ms` is the quantity used for regulation. `measured_latency_ms` is the user-facing total estimate.

### Target Fill

The target latency is converted to a target fill level:

- target fill = `target_latency_ms * input_sample_rate * channel_count / 1000`

This fill is the center of the controller.

## Shared Recovery State Machine

The recovery state machine exposes the UI states:

- `stable`
- `low-recover`
- `settling`
- `high-recover`

### Low Recovery

Low recovery is used when the buffer falls too far below target. It is
**enabled** by the `hard_recover_low_in_far_mode` switch (or by startup), and
entry into Refill triggers as soon as
`control_available < target - low_recover_entry_margin_ms` — the sole low-side
trigger (no longer gated by the far band, see Near/Far).

State progression:

1. `stable -> low-recover` (Refill)
2. `low-recover -> settling`
3. `settling -> stable`

During `low-recover` (Refill phase) output is muted and the ring refills. The
Refill exit is **predictive**: we move to `settling` as soon as
`control_available` — or its projection through the refill-speed EMA
`low_recover_refill_delta_ema` — reaches `target - low_recover_exit_margin_ms`.

**Throughout low-recover (both Refill and Settling) the PI servo is disabled**
(gated by `low_recover_phase == Inactive` in `pipewire.rs` and `asio.rs`) and
the ratio is pinned to the base ratio: no clock correction is applied until we
are back in `stable`.

### Settling

`settling` exists to avoid reopening audio immediately after refill. The goal is to make the effective returned latency less random.

Current behavior:

- output is muted if `force_silence_in_far_mode` is set (default), otherwise it becomes audible again during settling
- if the level falls below `target - low_recover_settle_margin_ms`, go back to `low-recover`
- if the level rises above `target + low_recover_settle_margin_ms`, trim the excess
- if the level stays inside the settling window long enough, transition to `stable`

Current exit timing:

- `low_recover_settle_stable_ms` (default `200 ms`) of **continuously** accumulated stable callback time; any excursion out of band re-arms the counter

Current settling half-window:

- `low_recover_settle_margin_ms` (default `6 ms`), converted to samples and aligned to the audio frame

#### Raw vs smoothed (sawtooth handling)

The settling dwell bounds are tested against the **smoothed** level
(`smoothed_control_available`, the same IIR low-pass the PI servo sees), **not**
the raw level. Entry/exit actions (Refill entry, predictive Refill exit,
hard-recover) stay on the **raw** level to remain responsive — low-passing them
re-added phase lag that previously drove a slow oscillation.

Why: bursty input arrival (decoder batching) puts a **sawtooth** on
`control_available` whose amplitude exceeds the `±low_recover_settle_margin_ms`
half-window. Judged on raw, every sawtooth tooth leaves the band and re-arms the
timer → warmup never reaches `stable` (whereas once in `stable`, the servo —
which already works on the smoothed level — holds fine). Judged on smoothed, the
sawtooth is absorbed and the dwell can mature.

On the `Refill -> Settling` transition the IIR state is **reset** so the
smoothed level restarts from the real current level instead of a value still
lagging behind the refill ramp (which would bounce straight back to Refill).

### High Recovery

High recovery is used when the buffer is too far above target.

Behavior:

- aggressively discard buffered audio while muted
- return toward target faster than the slow servo path

## Near/Far Band Logic

The `near/far` band is derived from buffer error relative to target:

- `near` if `abs(control_available - target_fill) < high_recover_entry_margin_ms`
- `far` otherwise

> Rename: `near_far_threshold_ms` is now **`high_recover_entry_margin_ms`**
> ("High-recover entry margin"), forming a clear pair with
> `low_recover_entry_margin_ms`. The threshold is **symmetric** (`abs_diff`),
> but for a realistic target latency it can only be reached on the **high**
> side, so it is the entry for high-side actions (hard-recover-high, far mute).
> The low-recover **entry** no longer uses this band: it triggers on
> `low_recover_entry_margin_ms` (low side) whenever `hard_recover_low_in_far_mode`
> (or startup) is active. The old names (on-disk config, JSON key
> `nearFarThresholdMs`, OSC address) are still accepted on read via aliases.

This band is used both for UI and for determining whether far-mode actions are eligible.

The important distinction is:

- the band tells us whether we are near or far from target
- the recovery state tells us what the recovery machine is currently doing

These are related, but not the same thing.

## Adaptive Local Resampling

When adaptive resampling is enabled, a PI servo nudges the local resampling ratio around the base ratio.

Shared logic lives in:

- [lib.rs](/home/user/dev/spatial-renderer/Omniphony/omniphony-renderer/audio_output/src/lib.rs)
- [adaptive_runtime.rs](/home/user/dev/spatial-renderer/Omniphony/omniphony-renderer/audio_output/src/adaptive_runtime.rs)

Inputs:

- current control fill
- target fill
- configured gains `kp_near`, `ki`
- `max_adjust`
- `integral_discharge_ratio`

Outputs:

- effective local resampling ratio
- displayed rate-adjust value
- current adaptive band (`near` or `far`)

The PI loop is only one part of the system. It does not replace hard recovery. It attempts to keep the system centered before hard recovery becomes necessary.

## Startup Behavior

### ASIO

ASIO startup now reuses the normal low-recovery state machine instead of using a dedicated pre-fill gate.

Current startup flow:

1. stream starts muted in `low-recover`
2. refill runs using the same logic as ordinary low-buffer recovery
3. `settling` stabilizes the returned latency
4. transition to `stable`

Additionally, when startup recovery finishes, ASIO explicitly resets:

- the local resampler internal state
- the resampler FIFO

and keeps one extra callback muted before the first audible block. This is intended to avoid startup transients leaking out of the local resampler state.

### PipeWire

PipeWire also forces the startup low-recover: `activate_startup_low_recover()` is called when the stream is created (`pipewire.rs`), exactly like ASIO. Startup therefore runs the same Refill → Settling → stable state machine. The difference from ASIO is the callback cadence (driven by the graph quantum) and the latency measurement, not the presence of the startup gate.

## ASIO / PipeWire Differences

This is the most important backend-specific section.

### 1. Callback Model

`ASIO`:

- callback size is determined by the driver/CPAL backend
- can be relatively coarse and backend-specific
- this makes threshold-based recovery more sensitive to callback granularity

`PipeWire`:

- callback cadence is tied to the graph quantum
- tends to be more regular
- makes settling and servo behavior easier to tune

### 2. Latency Measurement

`ASIO`:

- does not currently have a true backend graph-latency measurement
- uses a midpoint estimate based on callback size
- total displayed latency is therefore a model, not a direct driver-reported value

`PipeWire`:

- samples downstream graph latency via `pw_stream_get_time()`
- includes real graph scheduling delay in `measured_latency_ms`

This is why two backends can sound similarly stable while reporting different-looking latency numbers.

### 3. Non-Resampling Behavior

`ASIO`:

- without adaptive local resampling, it still relies on the shared far-mode recovery logic
- there is no separate backend-native servo equivalent to PipeWire's non-local-resampler path

`PipeWire`:

- has two regimes:
  - local resampler path
  - native backend rate/latency servo path when local resampling is not used

This makes PipeWire structurally more flexible, but also means the two backends are not exact mirrors.

### 4. Startup Strategy

`ASIO`:

- startup is now explicitly treated as low recovery
- mute/recovery/fade behavior is intentionally unified with ordinary low-buffer recovery

`PipeWire`:

- also forces the startup low-recover (`activate_startup_low_recover()`), same state machine as ASIO
- the graph callback cadence just makes refill/settling more regular to tune

### 5. Sensitivity to Thresholds

`ASIO` is more sensitive to:

- settling window size
- refill/settling transition thresholds
- startup transient cleanup

`PipeWire` is more sensitive to:

- graph quantum
- backend latency reporting
- the split between local resampler control and native backend rate control

## Current Practical Interpretation

When debugging the system, interpret states as follows:

- `stable`: no active recovery state machine
- `low-recover`: output is muted because the system is rebuilding latency from below target
- `settling`: the system confirms stability (on the smoothed level) before handing back to the servo; output is muted if `force_silence_in_far_mode` is set (default)
- `high-recover`: buffered audio is being dropped because latency is too high
- `near` / `far`: distance from target, not mute state by itself

If audio is wrong, always inspect both:

- band: `near` / `far`
- state: `stable` / `low-recover` / `settling` / `high-recover`

The band explains where the controller is relative to target. The state explains what the recovery machine is actively doing.
