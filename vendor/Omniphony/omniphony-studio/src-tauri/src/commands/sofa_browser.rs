//! Browse the sofacoustics.org SOFA database and download HRTF files.
//!
//! The site serves plain Apache directory indexes under a fixed root; we list
//! entries (subdirectories + `.sofa` files), download a chosen file into the
//! app data dir, and the frontend then activates it through the existing
//! `control_hrir_source` command (`sofa:<local path>`).

use std::io::{Read, Write};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};

use tauri::{Emitter, Manager};

use crate::osc_listener::OscControlMsg;

/// Fixed browse root. `path` arguments are relative to this and sanitised —
/// the browser can never escape it.
const ROOT: &str = "https://sofacoustics.org/data/";

/// Cancellation flag for the (single) in-flight download. The UI serialises
/// downloads, so one global flag is enough.
static CANCEL: AtomicBool = AtomicBool::new(false);

/// Progress event emitted to the webview every ~100 ms while downloading.
const PROGRESS_EVENT: &str = "sofa:download_progress";

#[derive(serde::Serialize)]
pub struct SofaEntry {
    /// Percent-encoded path segment as found in the index (append to the
    /// current path for navigation/download).
    pub href: String,
    /// Human-readable (percent-decoded) name.
    pub name: String,
    pub dir: bool,
    /// Size column as shown by the index ("1.6M", "-" for dirs).
    pub size: String,
}

fn percent_decode(s: &str) -> String {
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            if let Ok(v) = u8::from_str_radix(&s[i + 1..i + 3], 16) {
                out.push(v);
                i += 3;
                continue;
            }
        }
        out.push(bytes[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// Reject anything that could walk out of the root.
fn sanitize(rel: &str) -> Result<String, String> {
    let decoded = percent_decode(rel);
    if rel.starts_with('/') || rel.contains("://") {
        return Err("invalid path".into());
    }
    for seg in decoded.split('/') {
        if seg == ".." {
            return Err("invalid path".into());
        }
    }
    Ok(rel.to_string())
}

fn agent() -> ureq::Agent {
    ureq::AgentBuilder::new()
        .timeout_connect(std::time::Duration::from_secs(10))
        .timeout_read(std::time::Duration::from_secs(60))
        .build()
}

fn parse_index(html: &str) -> Vec<SofaEntry> {
    let mut entries = Vec::new();
    for line in html.lines() {
        let Some(h0) = line.find("href=\"") else {
            continue;
        };
        let rest = &line[h0 + 6..];
        let Some(h1) = rest.find('"') else { continue };
        let href = &rest[..h1];
        // Skip sort links, parent/absolute links, and external URLs.
        if href.is_empty()
            || href.starts_with('?')
            || href.starts_with('/')
            || href.starts_with('#')
            || href.contains("://")
            || href == "../"
        {
            continue;
        }
        let dir = href.ends_with('/');
        let name = percent_decode(href.trim_end_matches('/'));
        // Files: only .sofa is loadable; hide the rest (docs, meshes, csv…).
        if !dir && !name.to_ascii_lowercase().ends_with(".sofa") {
            continue;
        }
        // Size = second right-aligned cell of the row (first is the date).
        let mut sizes = line
            .split("<td align=\"right\">")
            .skip(2)
            .map(|c| c.split('<').next().unwrap_or("").trim().to_string());
        let size = sizes.next().unwrap_or_default();
        entries.push(SofaEntry {
            href: href.to_string(),
            name,
            dir,
            size,
        });
    }
    // Directories first, then files, each alphabetically.
    entries.sort_by(|a, b| b.dir.cmp(&a.dir).then(a.name.cmp(&b.name)));
    entries
}

/// List one directory of the SOFA database. `path` is the percent-encoded
/// path relative to the root ("" = root, "database/hutubs/" …).
#[tauri::command]
pub async fn sofa_browse(path: String) -> Result<Vec<SofaEntry>, String> {
    let rel = sanitize(&path)?;
    tauri::async_runtime::spawn_blocking(move || {
        let url = format!("{ROOT}{rel}");
        let body = agent()
            .get(&url)
            .call()
            .map_err(|e| format!("fetch {url}: {e}"))?
            .into_string()
            .map_err(|e| format!("read {url}: {e}"))?;
        Ok(parse_index(&body))
    })
    .await
    .map_err(|e| format!("task failed: {e}"))?
}

#[derive(serde::Serialize, serde::Deserialize, Clone, Default)]
pub struct SofaMeta {
    pub license: String,
    pub author: String,
    pub organization: String,
}

#[derive(serde::Serialize)]
pub struct LocalSofa {
    pub name: String,
    pub path: String,
    pub size: String,
    pub meta: SofaMeta,
}

/// Read the SOFA global attributes that matter for licensing/attribution.
/// Parsing a big file is not free, so the result is cached in a JSON sidecar
/// (`<file>.meta.json`) written next to the .sofa.
fn sofa_meta(path: &std::path::Path) -> SofaMeta {
    let sidecar = path.with_extension("sofa.meta.json");
    if let Ok(bytes) = std::fs::read(&sidecar) {
        if let Ok(meta) = serde_json::from_slice::<SofaMeta>(&bytes) {
            return meta;
        }
    }
    let meta = match sofar::reader::Sofar::open(path) {
        Ok(sofa) => {
            let attrs = &sofa.hrtf().attributes;
            let get = |k: &str| attrs.get(k).cloned().unwrap_or_default();
            SofaMeta {
                license: get("License"),
                author: get("AuthorContact"),
                organization: get("Organization"),
            }
        }
        Err(_) => SofaMeta::default(),
    };
    if let Ok(bytes) = serde_json::to_vec(&meta) {
        let _ = std::fs::write(&sidecar, bytes);
    }
    meta
}

/// Fetch (and cache) the license metadata of one local .sofa file.
#[tauri::command]
pub async fn sofa_file_meta(path: String) -> Result<SofaMeta, String> {
    tauri::async_runtime::spawn_blocking(move || Ok(sofa_meta(std::path::Path::new(&path))))
        .await
        .map_err(|e| format!("task failed: {e}"))?
}

/// List the already-downloaded `.sofa` files in `<app data dir>/hrtf/`.
/// This is the default browser view — no network involved.
#[tauri::command]
pub async fn sofa_list_local(app: tauri::AppHandle) -> Result<Vec<LocalSofa>, String> {
    let dir = app
        .path()
        .app_data_dir()
        .map_err(|e| format!("no app data dir: {e}"))?
        .join("hrtf");
    tauri::async_runtime::spawn_blocking(move || sofa_list_local_blocking(&dir))
        .await
        .map_err(|e| format!("task failed: {e}"))?
}

fn sofa_list_local_blocking(dir: &std::path::Path) -> Result<Vec<LocalSofa>, String> {
    let mut out = Vec::new();
    let entries = match std::fs::read_dir(dir) {
        Ok(e) => e,
        Err(_) => return Ok(out), // dir not created yet → empty list
    };
    for entry in entries.flatten() {
        let path = entry.path();
        let name = entry.file_name().to_string_lossy().into_owned();
        if !name.to_ascii_lowercase().ends_with(".sofa") {
            continue;
        }
        let size = entry
            .metadata()
            .map(|m| {
                let mb = m.len() as f64 / (1024.0 * 1024.0);
                if mb >= 1.0 {
                    format!("{mb:.1} MB")
                } else {
                    format!("{:.0} kB", m.len() as f64 / 1024.0)
                }
            })
            .unwrap_or_default();
        let meta = sofa_meta(&path);
        out.push(LocalSofa {
            name,
            path: path.to_string_lossy().into_owned(),
            size,
            meta,
        });
    }
    out.sort_by(|a, b| a.name.cmp(&b.name));
    Ok(out)
}

fn hrtf_dir(app: &tauri::AppHandle) -> Result<PathBuf, String> {
    Ok(app
        .path()
        .app_data_dir()
        .map_err(|e| format!("no app data dir: {e}"))?
        .join("hrtf"))
}

/// Delete cached .sofa files (and their metadata sidecars). Paths must live
/// inside the hrtf cache dir — anything else is rejected.
#[tauri::command]
pub fn sofa_delete_local(app: tauri::AppHandle, paths: Vec<String>) -> Result<(), String> {
    let dir = hrtf_dir(&app)?;
    for p in paths {
        let path = PathBuf::from(&p);
        if path.parent() != Some(dir.as_path()) {
            return Err(format!("refusing to delete outside the cache: {p}"));
        }
        if !p.to_ascii_lowercase().ends_with(".sofa") {
            return Err(format!("not a .sofa file: {p}"));
        }
        std::fs::remove_file(&path).map_err(|e| format!("delete {p}: {e}"))?;
        let _ = std::fs::remove_file(path.with_extension("sofa.meta.json"));
    }
    Ok(())
}

/// Copy a user-picked .sofa file from anywhere on this machine into the
/// cache dir (metadata sidecar is built on the next listing).
#[tauri::command]
pub async fn sofa_import_local(app: tauri::AppHandle, src: String) -> Result<String, String> {
    let dir = hrtf_dir(&app)?;
    tauri::async_runtime::spawn_blocking(move || {
        let srcp = PathBuf::from(&src);
        let name = srcp
            .file_name()
            .map(|f| f.to_string_lossy().into_owned())
            .unwrap_or_default();
        if !name.to_ascii_lowercase().ends_with(".sofa") {
            return Err(format!("not a .sofa file: {src}"));
        }
        std::fs::create_dir_all(&dir).map_err(|e| format!("create {dir:?}: {e}"))?;
        let dest = dir.join(&name);
        std::fs::copy(&srcp, &dest).map_err(|e| format!("copy {src}: {e}"))?;
        let _ = std::fs::remove_file(dest.with_extension("sofa.meta.json"));
        Ok(dest.to_string_lossy().into_owned())
    })
    .await
    .map_err(|e| format!("task failed: {e}"))?
}

/// Push a cached .sofa to the RENDERER over OSC (chunked blobs) — for setups
/// where Studio does not share a filesystem with the renderer. The renderer
/// stores it under its own config dir and activates it on completion.
#[tauri::command]
pub async fn sofa_upload_to_renderer(
    app: tauri::AppHandle,
    state: tauri::State<'_, crate::SharedState>,
    path: String,
) -> Result<u32, String> {
    let dir = hrtf_dir(&app)?;
    let p = PathBuf::from(&path);
    if p.parent() != Some(dir.as_path()) {
        return Err("upload source must be a cached file".into());
    }
    let name = p
        .file_name()
        .map(|f| f.to_string_lossy().into_owned())
        .unwrap_or_default();
    let data = std::fs::read(&p).map_err(|e| format!("read {path}: {e}"))?;
    let total = data.len();
    if total == 0 || total > (1 << 30) {
        return Err(format!("bad file size: {total}"));
    }
    const CHUNK: usize = 32 * 1024;
    let tx = state.osc_tx.clone();
    tauri::async_runtime::spawn_blocking(move || {
        crate::send_control(
            &tx,
            OscControlMsg::SendArgs {
                address: "/omniphony/control/binaural/hrtf_upload/begin".to_string(),
                args: vec![
                    rosc::OscType::String(name),
                    rosc::OscType::Int(total as i32),
                ],
            },
        );
        let mut seq: i32 = 0;
        for chunk in data.chunks(CHUNK) {
            crate::send_control(
                &tx,
                OscControlMsg::SendArgs {
                    address: "/omniphony/control/binaural/hrtf_upload/chunk".to_string(),
                    args: vec![rosc::OscType::Int(seq), rosc::OscType::Blob(chunk.to_vec())],
                },
            );
            seq += 1;
            // UDP politeness: don't burst hundreds of datagrams back-to-back.
            if seq % 16 == 0 {
                std::thread::sleep(std::time::Duration::from_millis(2));
            }
        }
        crate::send_control(
            &tx,
            OscControlMsg::SendArgs {
                address: "/omniphony/control/binaural/hrtf_upload/end".to_string(),
                args: vec![rosc::OscType::Int(seq)],
            },
        );
        Ok(seq as u32)
    })
    .await
    .map_err(|e| format!("task failed: {e}"))?
}

/// Abort the in-flight download (the `.part` file is removed).
#[tauri::command]
pub fn sofa_download_cancel() {
    CANCEL.store(true, Ordering::Relaxed);
}

/// Download one `.sofa` file into `<app data dir>/hrtf/` and return its local
/// path. The file name flattens the relative path so different databases
/// cannot collide. Streams in chunks, emitting `sofa:download_progress`
/// `{bytes, total}` events (~10 Hz) and honouring [`sofa_download_cancel`].
/// No size limit — some database entries are hundreds of MB; the progress
/// bar and the cancel button are the safety valve. An already-downloaded
/// file whose size matches the server's Content-Length is reused as-is.
#[tauri::command]
pub async fn sofa_download(app: tauri::AppHandle, path: String) -> Result<String, String> {
    let rel = sanitize(&path)?;
    if !percent_decode(&rel).to_ascii_lowercase().ends_with(".sofa") {
        return Err("not a .sofa file".into());
    }
    let dir = app
        .path()
        .app_data_dir()
        .map_err(|e| format!("no app data dir: {e}"))?
        .join("hrtf");
    tauri::async_runtime::spawn_blocking(move || {
        CANCEL.store(false, Ordering::Relaxed);
        std::fs::create_dir_all(&dir).map_err(|e| format!("create {dir:?}: {e}"))?;
        let flat = percent_decode(&rel).replace('/', "_");
        let dest: PathBuf = dir.join(flat);
        let url = format!("{ROOT}{rel}");
        let resp = agent()
            .get(&url)
            .call()
            .map_err(|e| format!("fetch {url}: {e}"))?;
        let total: Option<u64> = resp.header("Content-Length").and_then(|v| v.parse().ok());

        // Cached copy with the expected size → reuse, no network transfer.
        if let (Some(expected), Ok(meta)) = (total, std::fs::metadata(&dest)) {
            if meta.len() == expected {
                let _ = app.emit(
                    PROGRESS_EVENT,
                    serde_json::json!({ "bytes": expected, "total": expected }),
                );
                return Ok(dest.to_string_lossy().into_owned());
            }
        }

        let mut reader = resp.into_reader();
        let tmp = dest.with_extension("part");
        let mut file = std::fs::File::create(&tmp).map_err(|e| format!("create {tmp:?}: {e}"))?;
        let mut buf = vec![0u8; 256 * 1024];
        let mut done: u64 = 0;
        let mut last_emit = std::time::Instant::now();
        loop {
            if CANCEL.load(Ordering::Relaxed) {
                drop(file);
                let _ = std::fs::remove_file(&tmp);
                return Err("cancelled".into());
            }
            let n = reader.read(&mut buf).map_err(|e| {
                let _ = std::fs::remove_file(&tmp);
                format!("download {url}: {e}")
            })?;
            if n == 0 {
                break;
            }
            file.write_all(&buf[..n]).map_err(|e| {
                let _ = std::fs::remove_file(&tmp);
                format!("write {tmp:?}: {e}")
            })?;
            done += n as u64;
            if last_emit.elapsed() >= std::time::Duration::from_millis(100) {
                last_emit = std::time::Instant::now();
                let _ = app.emit(
                    PROGRESS_EVENT,
                    serde_json::json!({ "bytes": done, "total": total }),
                );
            }
        }
        let _ = app.emit(
            PROGRESS_EVENT,
            serde_json::json!({ "bytes": done, "total": total }),
        );
        std::fs::rename(&tmp, &dest).map_err(|e| format!("finalize {dest:?}: {e}"))?;
        Ok(dest.to_string_lossy().into_owned())
    })
    .await
    .map_err(|e| format!("task failed: {e}"))?
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_apache_index_rows() {
        let html = r#"
<tr><td><img alt="[PARENTDIR]"></td><td><a href="/data/">Parent Directory</a></td><td>&nbsp;</td><td align="right">  - </td></tr>
<tr><td><img alt="[DIR]"></td><td><a href="hutubs/">hutubs/</a></td><td align="right">2020-01-01 10:00  </td><td align="right">  - </td></tr>
<tr><td><img alt="[FILE]"></td><td><a href="pp1_HRIRs_measured.sofa">pp1_HRIRs_measured.sofa</a></td><td align="right">2020-01-01 10:00  </td><td align="right">1.6M</td></tr>
<tr><td><img alt="[FILE]"></td><td><a href="Documentation.pdf">Documentation.pdf</a></td><td align="right">2020-01-01 10:00  </td><td align="right">2M</td></tr>
<tr><th><a href="?C=N;O=D">Name</a></th></tr>
"#;
        let entries = parse_index(html);
        assert_eq!(entries.len(), 2);
        assert!(entries[0].dir && entries[0].name == "hutubs");
        assert!(!entries[1].dir && entries[1].name == "pp1_HRIRs_measured.sofa");
        assert_eq!(entries[1].size, "1.6M");
    }

    #[test]
    fn sanitize_rejects_escapes() {
        assert!(sanitize("../etc/").is_err());
        assert!(sanitize("a/%2e%2e/b").is_err());
        assert!(sanitize("/abs").is_err());
        assert!(sanitize("http://x").is_err());
        assert!(sanitize("database/hutubs/").is_ok());
    }

    #[test]
    fn decodes_percent_names() {
        assert_eq!(
            percent_decode("aachen%20(high-resolution)"),
            "aachen (high-resolution)"
        );
    }
}
