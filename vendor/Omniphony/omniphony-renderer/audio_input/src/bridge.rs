use anyhow::{Result, anyhow};
use bridge_api::{FormatBridgeBox, RDecodedFrame, RInputTransport};
use spdif::SpdifParser;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, RwLock, mpsc};
use std::thread;
use std::time::Instant;

pub struct LiveBridgeIngestRuntime {
    raw_tx: mpsc::SyncSender<(u8, Vec<u8>)>,
    spdif_parser: SpdifParser,
}

impl LiveBridgeIngestRuntime {
    pub fn new(raw_tx: mpsc::SyncSender<(u8, Vec<u8>)>) -> Self {
        Self {
            raw_tx,
            spdif_parser: SpdifParser::new(),
        }
    }

    pub fn process_chunk(&mut self, chunk: &[u8]) -> (usize, usize) {
        let mut packet_count = 0usize;
        let mut queued_count = 0usize;
        self.spdif_parser.push_bytes(chunk);
        while let Some(packet) = self.spdif_parser.get_next_packet() {
            packet_count += 1;
            if self
                .raw_tx
                .try_send((packet.data_type, packet.payload))
                .is_ok()
            {
                queued_count += 1;
            }
        }
        if packet_count > 0 {
            log::debug!(
                "LiveBridgeIngestRuntime: extracted {} SPDIF packet(s), queued {}",
                packet_count,
                queued_count
            );
            if queued_count < packet_count {
                log::warn!(
                    "LiveBridgeIngestRuntime dropped {} SPDIF packet(s): bridge decode queue is full or disconnected",
                    packet_count - queued_count
                );
            }
        }
        (packet_count, queued_count)
    }
}

pub struct BridgeDecodeDiag {
    /// Number of decoded frames returned by the most recent `push_packet`.
    pub frames_per_push_packet: Arc<AtomicU64>,
    /// Wall-clock interval between consecutive `push_packet` entries (µs).
    pub push_packet_dt_us: Arc<AtomicU64>,
}

pub fn spawn_bridge_decode_worker<OnFrame>(
    bridge: FormatBridgeBox,
    raw_rx: mpsc::Receiver<(u8, Vec<u8>)>,
    requested_drc_mode: Option<Arc<RwLock<String>>>,
    diag: Option<BridgeDecodeDiag>,
    mut on_frame: OnFrame,
) -> Result<thread::JoinHandle<()>>
where
    OnFrame: FnMut(RDecodedFrame, f32) + Send + 'static,
{
    thread::Builder::new()
        .name("bridge-decode".to_string())
        .spawn(move || {
            let mut bridge = bridge;
            let mut first_frame_logs_remaining = 16usize;
            let mut last_applied_drc_mode: Option<String> = None;
            let mut last_push_packet_at: Option<Instant> = None;
            while let Ok((data_type, payload)) = raw_rx.recv() {
                if let Some(state) = requested_drc_mode.as_ref() {
                    let current = state.read().unwrap().clone();
                    if last_applied_drc_mode.as_ref() != Some(&current) {
                        bridge.set_drc_mode(current.as_str().into());
                        last_applied_drc_mode = Some(current);
                    }
                }
                let decode_started_at = Instant::now();
                let push_packet_dt_us = last_push_packet_at
                    .map(|prev| {
                        decode_started_at
                            .saturating_duration_since(prev)
                            .as_micros() as u64
                    })
                    .unwrap_or(0);
                last_push_packet_at = Some(decode_started_at);
                let result = bridge.push_packet(
                    payload.as_slice().into(),
                    RInputTransport::Iec61937,
                    data_type,
                );
                let decode_time_ms = decode_started_at.elapsed().as_secs_f32() * 1000.0;
                if let Some(ref d) = diag {
                    d.frames_per_push_packet.store(
                        (result.frames.len() as f64).to_bits(),
                        Ordering::Relaxed,
                    );
                    d.push_packet_dt_us
                        .store((push_packet_dt_us as f64).to_bits(), Ordering::Relaxed);
                }
                if !result.error_message.is_empty() || result.did_reset {
                    log::warn!(
                        "PipeWire bridge packet: data_type=0x{:02X} payload_bytes={} frames={} reset={} error={}",
                        data_type,
                        payload.len(),
                        result.frames.len(),
                        result.did_reset,
                        result.error_message
                    );
                }
                // Bridge reset: keep audio running, never abort or flush. In live
                // rendering we never want a premature stop (strict mode removed).
                let frame_count = result.frames.len().max(1) as f32;
                let per_frame_decode_time_ms = decode_time_ms / frame_count;
                for frame in result.frames {
                    if first_frame_logs_remaining > 0 {
                        first_frame_logs_remaining -= 1;
                        let frame_ms =
                            frame.sample_count as f64 / frame.sampling_frequency.max(1) as f64
                                * 1000.0;
                        log::debug!(
                            "PipeWire bridge decoded frame: sr={} sample_count={} ch={} frame_ms={:.3} data_type=0x{:02X} payload_bytes={}",
                            frame.sampling_frequency,
                            frame.sample_count,
                            frame.channel_count,
                            frame_ms,
                            data_type,
                            payload.len()
                        );
                    }
                    on_frame(frame, per_frame_decode_time_ms);
                }
            }
        })
        .map_err(|e| anyhow!("Failed to spawn bridge decode worker: {e}"))
}
