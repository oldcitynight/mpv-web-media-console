//! Extension point for host-owned control domains (audio output/input).
//!
//! The audio-free core (this crate, inside `liborender`) handles the
//! renderer/layout/speakers/loudness/DRC OSC domains. A host that owns audio
//! (the `orender` CLI) implements [`HostControlHandler`] in the separate
//! `host_audio` crate and registers it with the engine's OSC server, which
//! delegates unrecognised control messages to it and folds its state into the
//! live-state snapshot and the saved config. Hosts without audio (mpv via
//! `liborender`) register nothing, so the core never references the audio crates.

use rosc::{OscMessage, OscPacket};

use crate::osc::ControlEffects;

pub trait HostControlHandler: Send + Sync {
    /// Handle a control message the core did not recognise (e.g.
    /// `/omniphony/control/audio/*`, `/omniphony/control/input/*`). Returns the
    /// resulting effects, or `None` if this handler does not own `addr`.
    fn handle(&self, addr: &str, msg: &OscMessage) -> Option<ControlEffects>;

    /// Extra state messages appended to the live-state snapshot bundle, e.g.
    /// `/omniphony/state/audio` and the live-input portion of
    /// `/omniphony/state/input`.
    fn extend_snapshot(&self) -> Vec<OscPacket>;

    /// Amend the config being saved with host-owned fields (output device,
    /// live input, adaptive resampling, latency target) before it is written.
    fn amend_saved_config(&self, render: &mut renderer::config::RenderConfig);

    /// Monotonic generation counter the engine's OSC listener polls to decide
    /// when to re-broadcast the live-state bundle. Bumps when host-owned state
    /// changed asynchronously (e.g. PipeWire device enumeration). Default `0`
    /// disables polling for hosts that don't need it (embedded).
    fn state_generation(&self) -> u64 {
        0
    }
}
