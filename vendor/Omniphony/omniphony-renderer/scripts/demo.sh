#!/usr/bin/env bash
#
# One-command "clone-and-hear" demo for the Omniphony spatial renderer.
#
# Builds the reference WAV bridge and the orender CLI (if needed), then renders
# the bundled rotating 7.1.4 demo through the binaural headphone stage — no
# external player, no proprietary decoder.
#
# Usage:
#   scripts/demo.sh                 # binaural → PipeWire (default)
#   scripts/demo.sh speakers        # 7.1.4 speaker render → PipeWire (no binaural)
#   scripts/demo.sh file            # binaural → ffplay (no audio device needed)
#
# The `file` mode pipes raw f32 stereo to ffplay:
#   orender ... --output-backend file --output-file - --output-file-format raw-f32 \
#     | ffplay -f f32le -ar 48000 -ac 2 -

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RENDERER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"     # omniphony-renderer (cargo workspace root)
REPO_ROOT="$(cd "$RENDERER_DIR/.." && pwd)"      # repo root (holds layouts/)

BRIDGE="$RENDERER_DIR/target/release/libreference_bridge.so"
ORENDER="$RENDERER_DIR/target/release/orender"
LAYOUT="$REPO_ROOT/layouts/7.1.4.yaml"
WAV="$RENDERER_DIR/assets/demo/spatial-demo.wav"
CONFIG="$RENDERER_DIR/assets/demo/demo.yaml"

MODE="${1:-binaural}"

# Run hermetically: isolate orender from any pre-existing per-user config
# (~/.config/omniphony/config.yaml). A machine already set up for live playback
# can otherwise inject an input/output mode that does not match this file-decode
# demo. A throwaway XDG_CONFIG_HOME guarantees clean defaults on every machine.
DEMO_CONFIG_HOME="$(mktemp -d)"
trap 'rm -rf "$DEMO_CONFIG_HOME"' EXIT
export XDG_CONFIG_HOME="$DEMO_CONFIG_HOME"

echo "[demo] building reference bridge + orender (release) ..."
( cd "$RENDERER_DIR" && cargo build -r -p reference_bridge && cargo build -r -p omniphony-renderer )

if [[ ! -f "$WAV" ]]; then
  echo "[demo] generating demo asset ..."
  ( cd "$RENDERER_DIR" && cargo run -r -p reference_bridge --example gen_demo_wav )
fi

COMMON=(
  "$WAV"
  --bridge-path "$BRIDGE"
  --enable-vbap
  --speaker-layout "$LAYOUT"
)

case "$MODE" in
  binaural)
    echo "[demo] binaural → PipeWire"
    exec "$ORENDER" "${COMMON[@]}" --config "$CONFIG" --output-backend pipewire
    ;;
  speakers)
    echo "[demo] 7.1.4 speaker render → PipeWire"
    exec "$ORENDER" "${COMMON[@]}" --output-backend pipewire
    ;;
  file)
    echo "[demo] binaural → ffplay (no audio device needed)"
    "$ORENDER" "${COMMON[@]}" --config "$CONFIG" \
      --output-backend file --output-file - --output-file-format raw-f32 \
      | ffplay -hide_banner -autoexit -f f32le -ar 48000 -ac 2 -
    ;;
  *)
    echo "unknown mode: $MODE (expected: binaural | speakers | file)" >&2
    exit 2
    ;;
esac
