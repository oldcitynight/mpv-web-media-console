//! Adaptive-resampling and latency-target controls.
//!
//! Each command forwards a single value to the renderer over OSC. See the
//! adaptive resampling regulator on the renderer side for the meaning of each
//! parameter.

use crate::osc_listener::OscControlMsg;
use crate::{send_control, SharedState};
use tauri::State;

#[tauri::command]
pub fn control_adaptive_resampling(state: State<SharedState>, enable: i32) {
    send_control(
        &state.osc_tx,
        OscControlMsg::SendInt {
            address: "/omniphony/control/adaptive_resampling".to_string(),
            value: if enable != 0 { 1 } else { 0 },
        },
    );
}

#[tauri::command]
pub fn control_adaptive_resampling_enable_far_mode(state: State<SharedState>, enable: i32) {
    send_control(
        &state.osc_tx,
        OscControlMsg::SendInt {
            address: "/omniphony/control/adaptive_resampling/enable_far_mode".to_string(),
            value: if enable != 0 { 1 } else { 0 },
        },
    );
}

#[tauri::command]
pub fn control_adaptive_resampling_force_silence_in_far_mode(
    state: State<SharedState>,
    enable: i32,
) {
    send_control(
        &state.osc_tx,
        OscControlMsg::SendInt {
            address: "/omniphony/control/adaptive_resampling/force_silence_in_far_mode".to_string(),
            value: if enable != 0 { 1 } else { 0 },
        },
    );
}

#[tauri::command]
pub fn control_adaptive_resampling_hard_recover_high_in_far_mode(
    state: State<SharedState>,
    enable: i32,
) {
    send_control(
        &state.osc_tx,
        OscControlMsg::SendInt {
            address: "/omniphony/control/adaptive_resampling/hard_recover_high_in_far_mode"
                .to_string(),
            value: if enable != 0 { 1 } else { 0 },
        },
    );
}

#[tauri::command]
pub fn control_adaptive_resampling_hard_recover_low_in_far_mode(
    state: State<SharedState>,
    enable: i32,
) {
    send_control(
        &state.osc_tx,
        OscControlMsg::SendInt {
            address: "/omniphony/control/adaptive_resampling/hard_recover_low_in_far_mode"
                .to_string(),
            value: if enable != 0 { 1 } else { 0 },
        },
    );
}

#[tauri::command]
pub fn control_adaptive_resampling_far_mode_return_fade_in_ms(
    state: State<SharedState>,
    value: i32,
) {
    send_control(
        &state.osc_tx,
        OscControlMsg::SendInt {
            address: "/omniphony/control/adaptive_resampling/far_mode_return_fade_in_ms"
                .to_string(),
            value: value.max(0),
        },
    );
}

#[tauri::command]
pub fn control_latency_target(state: State<SharedState>, value: i32) {
    send_control(
        &state.osc_tx,
        OscControlMsg::SendInt {
            address: "/omniphony/control/latency_target".to_string(),
            value: value.max(1),
        },
    );
}

#[tauri::command]
pub fn control_adaptive_resampling_kp_near(state: State<SharedState>, value: f32) {
    send_control(
        &state.osc_tx,
        OscControlMsg::SendFloat {
            address: "/omniphony/control/adaptive_resampling/kp_near".to_string(),
            value: value.max(0.00000001),
        },
    );
}

#[tauri::command]
pub fn control_adaptive_resampling_ki(state: State<SharedState>, value: f32) {
    send_control(
        &state.osc_tx,
        OscControlMsg::SendFloat {
            address: "/omniphony/control/adaptive_resampling/ki".to_string(),
            value: value.max(0.00000001),
        },
    );
}

#[tauri::command]
pub fn control_adaptive_resampling_integral_discharge_ratio(state: State<SharedState>, value: f32) {
    send_control(
        &state.osc_tx,
        OscControlMsg::SendFloat {
            address: "/omniphony/control/adaptive_resampling/integral_discharge_ratio".to_string(),
            value: value.clamp(0.0, 1.0),
        },
    );
}

#[tauri::command]
pub fn control_adaptive_resampling_max_adjust(state: State<SharedState>, value: f32) {
    send_control(
        &state.osc_tx,
        OscControlMsg::SendFloat {
            address: "/omniphony/control/adaptive_resampling/max_adjust".to_string(),
            value: value.max(0.000001),
        },
    );
}

#[tauri::command]
pub fn control_adaptive_resampling_update_interval_callbacks(
    state: State<SharedState>,
    value: i32,
) {
    send_control(
        &state.osc_tx,
        OscControlMsg::SendInt {
            address: "/omniphony/control/adaptive_resampling/update_interval_callbacks".to_string(),
            value: value.max(1),
        },
    );
}

#[tauri::command]
pub fn control_adaptive_resampling_high_recover_entry_margin_ms(
    state: State<SharedState>,
    value: i32,
) {
    send_control(
        &state.osc_tx,
        OscControlMsg::SendInt {
            address: "/omniphony/control/adaptive_resampling/high_recover_entry_margin_ms"
                .to_string(),
            value: value.max(1),
        },
    );
}

#[tauri::command]
pub fn control_adaptive_resampling_pause(state: State<SharedState>, enable: i32) {
    send_control(
        &state.osc_tx,
        OscControlMsg::SendInt {
            address: "/omniphony/control/adaptive_resampling/pause".to_string(),
            value: if enable != 0 { 1 } else { 0 },
        },
    );
}

#[tauri::command]
pub fn control_adaptive_resampling_reset_ratio(state: State<SharedState>) {
    send_control(
        &state.osc_tx,
        OscControlMsg::SendInt {
            address: "/omniphony/control/adaptive_resampling/reset_ratio".to_string(),
            value: 1,
        },
    );
}
