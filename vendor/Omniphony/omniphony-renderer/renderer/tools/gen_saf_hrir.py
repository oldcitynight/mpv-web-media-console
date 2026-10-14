#!/usr/bin/env python3
"""Convert the SAF default HRIR set (ISC, Genelec Aural ID of a KEMAR dummy head
@48 kHz) into the compact binary blob the renderer embeds.

Source arrays (in saf_default_hrirs.c):
    const float __default_hrirs[836][2][256];      // [dir][ear][tap]
    const float __default_hrir_dirs_deg[836][2];   // [dir]{azimuth, elevation}

Processing per ear IR:
  * onset-align (remove bulk delay) so all FIRs are time-aligned — the renderer
    supplies the interaural delay analytically, and time alignment lets the grid
    be interpolated without comb-filtering;
  * truncate/window to HRIR_LEN taps.

Azimuth is converted from the SAF convention (counter-clockwise, +90 = left) to
the renderer convention (+az = right) by negating.

Blob layout (little-endian):
    magic   u32  = 0x4F484952 ('OHIR')
    version u32  = 1
    count   u32  = number of directions
    ir_len  u32  = taps per ear (== HRIR_LEN)
    fs      u32  = sample rate (Hz)
    then `count` records: az f32, el f32, left[ir_len] f32, right[ir_len] f32
"""
import re
import struct
import sys
from pathlib import Path

HRIR_LEN = 128          # must match hrir::HRIR_LEN
PRE_SAMPLES = 8         # samples kept before the onset
ONSET_FRAC = 0.15       # onset = first |x| above this fraction of the peak

FLOAT_RE = re.compile(r"-?\d+\.\d+(?:[eE][-+]?\d+)?")


def slice_between(text, start_marker, end_marker):
    i = text.index(start_marker)
    j = text.index(end_marker, i)
    return text[i:j]


def align_and_truncate(ir):
    peak = max(abs(x) for x in ir) or 1.0
    thresh = ONSET_FRAC * peak
    onset = next((k for k, x in enumerate(ir) if abs(x) >= thresh), 0)
    start = max(0, onset - PRE_SAMPLES)
    out = ir[start:start + HRIR_LEN]
    out += [0.0] * (HRIR_LEN - len(out))
    return out


def main():
    if len(sys.argv) != 3:
        print("usage: gen_saf_hrir.py <saf_default_hrirs.c> <out.bin>", file=sys.stderr)
        return 2
    src = Path(sys.argv[1]).read_text()

    ir_text = slice_between(src, "__default_hrirs[836][2][256]", "__default_hrir_dirs_deg")
    dir_text = slice_between(src, "__default_hrir_dirs_deg[836][2]", "__default_N_hrir_dirs")

    irs = [float(m.group()) for m in FLOAT_RE.finditer(ir_text)]
    dirs = [float(m.group()) for m in FLOAT_RE.finditer(dir_text)]

    n = len(dirs) // 2
    assert len(irs) == n * 2 * 256, (len(irs), n)
    assert len(dirs) == n * 2

    out = bytearray()
    out += struct.pack("<IIIII", 0x4F484952, 1, n, HRIR_LEN, 48000)
    for d in range(n):
        saf_az = dirs[d * 2]
        el = dirs[d * 2 + 1]
        az = (-saf_az) % 360.0
        base = d * 2 * 256
        left = align_and_truncate(irs[base:base + 256])
        right = align_and_truncate(irs[base + 256:base + 512])
        out += struct.pack("<ff", az, el)
        out += struct.pack(f"<{HRIR_LEN}f", *left)
        out += struct.pack(f"<{HRIR_LEN}f", *right)

    Path(sys.argv[2]).write_bytes(out)
    print(f"wrote {n} directions, {HRIR_LEN} taps → {sys.argv[2]} ({len(out)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
