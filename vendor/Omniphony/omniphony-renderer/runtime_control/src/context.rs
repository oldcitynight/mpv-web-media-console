use std::sync::Arc;

use renderer::live_params::RendererControl;

/// Per-message context for the core OSC dispatch (`apply_simple_osc_control`).
/// The audio-free core only needs the renderer control; audio-output/audio-input
/// live in `host_audio::HostAudio` and reach the OSC server via
/// [`crate::HostControlHandler`].
#[derive(Clone)]
pub struct RuntimeControlContext {
    pub renderer: Arc<RendererControl>,
}

impl RuntimeControlContext {
    pub fn new(renderer: Arc<RendererControl>) -> Self {
        Self { renderer }
    }
}
