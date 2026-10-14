//! Generate VBAP gain tables from speaker layout configuration.
//!
//! Obsolete: precomputed evaluation tables are no longer serialized to / loaded
//! from `.vbap` files. The gain backend is geometry-only (it computes gains at a
//! position) and every precomputed table is built at runtime in the evaluation
//! layer; the from-file loader was removed (see `orender_engine::renderer_build`,
//! which bails on `--vbap-table`). This command is kept only so the CLI surface
//! stays stable, and reports the removal.

use anyhow::{Result, bail};

use super::command::GenerateVbapArgs;

/// Execute the generate-vbap command.
///
/// Returns an error explaining that precomputed `.vbap` tables are no longer
/// supported — there is no loader for them anymore, so generating one would
/// produce an unusable file.
pub fn cmd_generate_vbap(_args: &GenerateVbapArgs) -> Result<()> {
    bail!(
        "generate-vbap is no longer supported: the VBAP backend is geometry-only and \
         precomputed tables are built at runtime in the evaluation layer (the .vbap \
         file format has no loader anymore)."
    )
}
