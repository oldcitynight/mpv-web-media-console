# Public upstream patch provenance

The included Omniphony source starts from upstream commit `b81f518831e89864a6391744cf9daa9eeadb3c51`. The original source-tree SHA-256 is `05dedad8a75ecb65497c900b3e264ef32a9c16929ab7cae7fc082f653c6cece6`. Its original licenses and notices are preserved.

The first public-only change is applied: `packaging/patches/orender-finite-eof.patch` (SHA-256 `686170bd3d76e7a86ce4a73751788bba77d8601c7955d7244b714813537864b6`). It changes only `omniphony-renderer/src/cli/decode/session_run.rs`: after a finite decoder thread finishes and the receive queue is empty, the CLI consumer leaves its polling loop and reaches normal output finalization. Continuous streams retain their existing behavior. No decoder, renderer algorithm, C ABI or bridge implementation is added or replaced.

The resulting Omniphony tree SHA-256 is `248c3935762a72ebc5e2e3d3ea79444b85de1b784e140315f8edb1640d965c6d`. Reverse-applying the included patch reproduces the original source-tree hash. The modified upstream file remains under its original project license, GPL-3.0-or-later. The application root license does not replace any upstream license.

The original CLI could finish decoding yet wait indefinitely because another sender kept the channel connected. The change was independently checked with finite WAV files, closed stdin, an open stream followed by EOF, continuous cancellation and exact output flushing. The original finite-input liveness change does not validate malformed input. Native system libraries and separately supplied decoder bridges require their own distribution and compatibility review.

## Finite WAV validation repair

A second public-only patch, `packaging/patches/orender-finite-validation.patch` (SHA-256 `d075a9e49d832ef29ecc99202e0a1be5804996e5b3f64a1c98630194b7b1bb5d`), changes only the public reference bridge and CLI decoder thread. It negotiates finite input validation through existing `configure` keys, `finite-input-validation=true` and `finite-input-eof`. No bridge/C ABI layout or method is added. Unsupported bridges retain existing behavior. The reference bridge rejects invalid headers, truncated declared data/RIFF content and incomplete sample frames at a normal finite EOF. Complete empty WAV and unknown-size aligned WAV streams remain valid. Continuous streams do not opt in; cancellation, reload and a closed output skip EOF validation.

The combined public upstream tree SHA-256 is `107dec17c087bab4b5a0b069db92e128f04f1b6faf0f9da0c32fa4a553bede56`. Reverse the validation patch first, then the original finite-EOF patch, to recover the original pinned source. All licenses remain unchanged.
