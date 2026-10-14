# Third-party source and dependency notices

## Included upstream source

`vendor/Omniphony/` derives from upstream commit `b81f518831e89864a6391744cf9daa9eeadb3c51`. The original normalized tree SHA-256 is `05dedad8a75ecb65497c900b3e264ef32a9c16929ab7cae7fc082f653c6cece6`; the included tree is `107dec17c087bab4b5a0b069db92e128f04f1b6faf0f9da0c32fa4a553bede56` after the public finite-EOF and finite-WAV-validation fixes documented in `UPSTREAM_PATCHES.md`. Preserve all original copyright, license and component notices.

The upstream project and renderer crates declare **GPL-3.0-or-later**. `omniphony-renderer/reference_bridge/Cargo.toml` declares **GPL-3.0-only**. The Studio Tauri manifest has no separate package-license field; the project-wide GPL notice is retained, and no separate permissive grant is inferred. The SAF/KEMAR HRIR resource has its original **ISC** notice and dataset attribution at `omniphony-renderer/renderer/src/binaural/data/saf_kemar.LICENSE` (Copyright 2020 Leo McCormack).

The snapshot includes the original upstream build inputs, resources, examples and tests plus the documented public finite-input fixes. It includes no private renderer or decoder extension and no native binary.

## Declared dependencies

Exact declared Python versions identify FastAPI 0.141.1 and redis-py 8.1.0 as MIT, Uvicorn 0.53.0 as BSD-3-Clause, and python-mpv 1.0.8 as GPLv2-or-later OR LGPLv2.1-or-later. Preserve the license files supplied with those packages when distributing them. Vue 3.5.43 and its shared, reactivity, runtime-core and runtime-dom packages are MIT licensed. Their upstream MIT license is preserved at `licenses/frontend/Vue-LICENSE` and included with the built frontend. Build dependencies retain their own licenses.

The application uses separately installed libmpv and operating-system audio/video components. This source snapshot does not bundle libmpv, Redis, Python distributions or dependency installations. A distribution containing native binaries needs the inventory and corresponding-source obligations for its exact native build and complete transitive dependency set.

## Application license

Application-owned code is **GPL-3.0-only**, as selected by the project owner; see the root `LICENSE`. All included upstream components retain the exact grants above. This does not grant later-GPL rights for the reference bridge or replace permissive component attribution requirements.
