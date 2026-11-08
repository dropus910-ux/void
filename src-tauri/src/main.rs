#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde::Serialize;
use serde_json::json;
use std::{
    collections::HashSet,
    fs,
    io::{Read, Seek, SeekFrom, Write},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::{atomic::{AtomicBool, Ordering}, OnceLock},
    time::{SystemTime, UNIX_EPOCH},
};
use tauri::{ipc::Response, AppHandle, Emitter, Manager};

#[cfg(windows)]
use std::os::windows::process::CommandExt;

#[derive(Serialize)]
struct Item {
    path: String,
    rel: String,
    size: u64,
}

#[derive(Serialize)]
struct Scan {
    audio: Vec<Item>,
    covers: Vec<Item>,
}

const AUDIO: &[&str] = &["mp3", "m4a", "flac", "ogg", "opus", "wav", "aac", "wma"];
const COVER_NAMES: &[&str] = &["cover", "folder", "front", "album", "artwork"];
const COVER_EXT: &[&str] = &["jpg", "jpeg", "png", "webp"];

// where the last-used music folder is remembered (in the app's config dir)
fn cfg_file(app: &AppHandle) -> Result<PathBuf, String> {
    let dir = app.path().app_config_dir().map_err(|e| e.to_string())?;
    fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    Ok(dir.join("last_folder.txt"))
}

fn walk(dir: &Path, root: &str, base: &Path, out: &mut Scan, depth: u32) {
    if depth > 12 {
        return;
    }
    let Ok(rd) = fs::read_dir(dir) else { return };
    for e in rd.flatten() {
        let p = e.path();
        let Ok(md) = e.metadata() else { continue };
        if md.is_dir() {
            walk(&p, root, base, out, depth + 1);
            continue;
        }
        let ext = p.extension().and_then(|x| x.to_str()).unwrap_or("").to_lowercase();
        let stem = p.file_stem().and_then(|x| x.to_str()).unwrap_or("").to_lowercase();
        let rel = format!(
            "{}/{}",
            root,
            p.strip_prefix(base).unwrap_or(&p).to_string_lossy().replace('\\', "/")
        );
        let item = Item { path: p.to_string_lossy().into_owned(), rel, size: md.len() };
        if AUDIO.contains(&ext.as_str()) {
            out.audio.push(item);
        } else if COVER_EXT.contains(&ext.as_str()) && COVER_NAMES.contains(&stem.as_str()) {
            out.covers.push(item);
        }
    }
}

fn audio_items(base: &Path) -> Vec<Item> {
    let root = base
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_else(|| "Music".into());
    let mut out = Scan { audio: vec![], covers: vec![] };
    walk(base, &root, base, &mut out, 0);
    out.audio
}

#[tauri::command]
fn last_folder(app: AppHandle) -> Option<String> {
    let s = fs::read_to_string(cfg_file(&app).ok()?).ok()?;
    let s = s.trim().to_string();
    if !s.is_empty() && Path::new(&s).is_dir() { Some(s) } else { None }
}

#[tauri::command]
async fn scan(app: AppHandle, path: String) -> Result<Scan, String> {
    let base = PathBuf::from(&path);
    if !base.is_dir() {
        return Err("not a folder".into());
    }
    let root = base
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_else(|| "Music".into());
    let mut out = Scan { audio: vec![], covers: vec![] };
    walk(&base, &root, &base, &mut out, 0);
    // Remember every valid selected folder, even if it is currently empty.
    let _ = fs::write(cfg_file(&app)?, &path);
    Ok(out)
}

#[tauri::command]
async fn read_range(path: String, start: u64, len: u64) -> Result<Response, String> {
    let mut f = fs::File::open(&path).map_err(|e| e.to_string())?;
    f.seek(SeekFrom::Start(start)).map_err(|e| e.to_string())?;
    let mut buf = Vec::new();
    f.take(len).read_to_end(&mut buf).map_err(|e| e.to_string())?;
    Ok(Response::new(buf))
}

// =====================================================================
//  spotDL integration
//  - first run: downloads spotdl.exe, deno.exe, ffmpeg.exe into
//    %LOCALAPPDATA%\com.glassplayer.app\bin
//  - download: runs spotdl in a visible terminal window or hidden
//    (hidden mode streams the output to the page)
// =====================================================================

const CREATE_NO_WINDOW: u32 = 0x0800_0000;
// Spotify playlists/albums -> one folder named after the playlist/album, all songs inside.
// Single songs (and other links) -> saved straight into the music folder.
fn output_template(url: &str) -> &'static str {
    if url.starts_with("https://open.spotify.com/")
        && (url.contains("/playlist/") || url.contains("/album/"))
    {
        "{list-name}/{artists} - {title}.{output-ext}"
    } else {
        "{artists} - {title}.{output-ext}"
    }
}
const DENO_URL: &str =
    "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip";
const FFMPEG_URL: &str =
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip";

// tell the page what is happening (kind: status | progress | log)
fn say(app: &AppHandle, kind: &str, msg: &str, pct: Option<f64>) {
    let _ = app.emit("spotdl", json!({ "kind": kind, "msg": msg, "pct": pct }));
}

fn data_dir(app: &AppHandle) -> Result<PathBuf, String> {
    let d = app.path().app_local_data_dir().map_err(|e| e.to_string())?;
    fs::create_dir_all(&d).map_err(|e| e.to_string())?;
    Ok(d)
}

fn bin_dir(app: &AppHandle) -> Result<PathBuf, String> {
    let d = data_dir(app)?.join("bin");
    fs::create_dir_all(&d).map_err(|e| e.to_string())?;
    Ok(d)
}

fn http() -> Result<reqwest::Client, String> {
    reqwest::Client::builder()
        .user_agent("VOID-player")
        .connect_timeout(std::time::Duration::from_secs(20))
        .build()
        .map_err(|e| e.to_string())
}

// download a file, reporting progress between `base`% and `base+span`%
async fn download(
    app: &AppHandle,
    url: &str,
    dest: &Path,
    label: &str,
    base: f64,
    span: f64,
) -> Result<(), String> {
    let mut resp = http()?
        .get(url)
        .send()
        .await
        .map_err(|e| format!("{label}: {e}"))?
        .error_for_status()
        .map_err(|e| format!("{label}: {e}"))?;
    let total = resp.content_length();
    let part = dest.with_extension("part");
    let mut f = fs::File::create(&part).map_err(|e| e.to_string())?;
    let mut got: u64 = 0;
    let mut last: i64 = -1;
    while let Some(c) = resp.chunk().await.map_err(|e| format!("{label}: {e}"))? {
        f.write_all(&c).map_err(|e| e.to_string())?;
        got += c.len() as u64;
        if let Some(t) = total {
            if t > 0 {
                let frac = got as f64 / t as f64;
                let pct = base + span * frac;
                if pct as i64 != last {
                    last = pct as i64;
                    say(app, "progress", &format!("Downloading {label}\u{2026} {:.0}%", frac * 100.0), Some(pct));
                }
            }
        }
    }
    drop(f);
    fs::rename(&part, dest).map_err(|e| e.to_string())
}

// find the Windows spotdl.exe in the latest GitHub release
async fn spotdl_asset_url() -> Result<String, String> {
    let v: serde_json::Value = http()?
        .get("https://api.github.com/repos/spotDL/spotify-downloader/releases/latest")
        .header("Accept", "application/vnd.github+json")
        .send()
        .await
        .map_err(|e| format!("spotDL release lookup: {e}"))?
        .error_for_status()
        .map_err(|e| format!("spotDL release lookup: {e}"))?
        .json()
        .await
        .map_err(|e| e.to_string())?;
    let assets = v["assets"].as_array().ok_or("no files in the spotDL release")?;
    for a in assets {
        let name = a["name"].as_str().unwrap_or("").to_lowercase();
        if name.contains("spotdl") && name.contains("win") && name.ends_with(".exe") {
            if let Some(u) = a["browser_download_url"].as_str() {
                return Ok(u.to_string());
            }
        }
    }
    Err("no Windows spotdl.exe found in the latest spotDL release".into())
}

// pull one file (matched by the end of its path) out of a zip
fn unzip_one(zip_path: &Path, suffix: &str, dest: &Path) -> Result<(), String> {
    let f = fs::File::open(zip_path).map_err(|e| e.to_string())?;
    let mut z = zip::ZipArchive::new(f).map_err(|e| e.to_string())?;
    for i in 0..z.len() {
        let mut ent = z.by_index(i).map_err(|e| e.to_string())?;
        let name = ent.name().replace('\\', "/").to_lowercase();
        if !ent.is_dir() && name.ends_with(suffix) {
            let part = dest.with_extension("part");
            let mut out = fs::File::create(&part).map_err(|e| e.to_string())?;
            std::io::copy(&mut ent, &mut out).map_err(|e| e.to_string())?;
            drop(out);
            return fs::rename(&part, dest).map_err(|e| e.to_string());
        }
    }
    Err(format!("{suffix} was not inside the zip"))
}

// make sure spotdl + deno + ffmpeg exist (downloads whatever is missing)
async fn ensure_tools(app: &AppHandle) -> Result<PathBuf, String> {
    let bin = bin_dir(app)?;
    let sp = bin.join("spotdl.exe");
    let de = bin.join("deno.exe");
    let ff = bin.join("ffmpeg.exe");
    if sp.exists() && de.exists() && ff.exists() {
        return Ok(bin);
    }
    say(app, "status", "First-time setup: getting spotDL, deno and ffmpeg\u{2026}", Some(0.0));
    if !sp.exists() {
        let u = spotdl_asset_url().await?;
        download(app, &u, &sp, "spotDL", 0.0, 12.0).await?;
    }
    if !de.exists() {
        let z = bin.join("deno.zip");
        download(app, DENO_URL, &z, "deno", 12.0, 13.0).await?;
        say(app, "status", "Unpacking deno\u{2026}", Some(25.0));
        unzip_one(&z, "deno.exe", &de)?;
        let _ = fs::remove_file(&z);
    }
    if !ff.exists() {
        let z = bin.join("ffmpeg.zip");
        download(app, FFMPEG_URL, &z, "ffmpeg", 25.0, 65.0).await?;
        say(app, "status", "Unpacking ffmpeg\u{2026}", Some(90.0));
        unzip_one(&z, "/bin/ffmpeg.exe", &ff)?;
        let _ = fs::remove_file(&z);
    }
    say(app, "progress", "Ready", Some(100.0));
    Ok(bin)
}

#[derive(Serialize)]
struct ToolsStatus {
    dir: String,
    spotdl: bool,
    deno: bool,
    ffmpeg: bool,
}

// which of the three tools are already on disk (the page shows this in its log)
#[tauri::command]
fn spotdl_tools_status(app: AppHandle) -> Result<ToolsStatus, String> {
    let b = bin_dir(&app)?;
    Ok(ToolsStatus {
        dir: b.display().to_string(),
        spotdl: b.join("spotdl.exe").exists(),
        deno: b.join("deno.exe").exists(),
        ffmpeg: b.join("ffmpeg.exe").exists(),
    })
}

#[tauri::command]
async fn spotdl_setup(app: AppHandle) -> Result<(), String> {
    ensure_tools(&app).await.map(|_| ())
}

// a https link made of plain characters, or a song-name search (spotDL searches for it).
// no quotes/backslashes/control characters, so the .bat script below stays injection-proof
fn valid_url(u: &str) -> bool {
    if u.starts_with("https://") {
        return u.len() < 500
            && u.chars().all(|c| c.is_ascii_alphanumeric() || ":/?=&._-%+~#,".contains(c));
    }
    !u.is_empty()
        && u.chars().count() <= 200
        && !u.starts_with('-')
        && !u.starts_with("http")
        && u.chars().all(|c| !c.is_control() && c != '"' && c != '\\')
}

fn strip_ansi(s: &str) -> String {
    let mut o = String::with_capacity(s.len());
    let mut it = s.chars().peekable();
    while let Some(c) = it.next() {
        if c == '\u{1b}' {
            if it.peek() == Some(&'[') {
                it.next();
                while let Some(&n) = it.peek() {
                    it.next();
                    if n.is_ascii_alphabetic() {
                        break;
                    }
                }
            }
        } else {
            o.push(c);
        }
    }
    o
}

fn flush_line(app: &AppHandle, line: &mut Vec<u8>) {
    if line.is_empty() {
        return;
    }
    let s = strip_ansi(&String::from_utf8_lossy(line));
    line.clear();
    let s = s.trim();
    if !s.is_empty() {
        say(app, "log", s, None);
    }
}

// read a pipe and forward every line (progress bars use \r, so split on both)
fn pump<R: Read>(app: &AppHandle, mut r: R) {
    let mut buf = [0u8; 4096];
    let mut line: Vec<u8> = Vec::new();
    loop {
        let n = match r.read(&mut buf) {
            Ok(0) | Err(_) => break,
            Ok(n) => n,
        };
        for &b in &buf[..n] {
            if b == b'\n' || b == b'\r' {
                flush_line(app, &mut line);
            } else {
                line.push(b);
            }
        }
    }
    flush_line(app, &mut line);
}

// hidden: no window, output streamed into VOID
fn run_hidden(app: &AppHandle, bin: &Path, out: &Path, url: &str) -> Result<(), String> {
    let path_env = format!("{};{}", bin.display(), std::env::var("PATH").unwrap_or_default());
    let mut cmd = Command::new(bin.join("spotdl.exe"));
    cmd.current_dir(out)
        .env("PATH", &path_env)
        .env("PYTHONIOENCODING", "utf-8")
        .env("PYTHONUTF8", "1")
        .env("NO_COLOR", "1")
        .arg("download")
        .arg(url)
        .arg("--ffmpeg")
        .arg(bin.join("ffmpeg.exe"))
        .arg("--output")
        .arg(output_template(url))
        .arg("--threads")
        .arg("8")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    #[cfg(windows)]
    cmd.creation_flags(CREATE_NO_WINDOW);

    let mut child = cmd.spawn().map_err(|e| format!("Couldn't start spotDL: {e}"))?;
    let so = child.stdout.take();
    let se = child.stderr.take();
    let a1 = app.clone();
    let a2 = app.clone();
    let t1 = std::thread::spawn(move || {
        if let Some(r) = so {
            pump(&a1, r);
        }
    });
    let t2 = std::thread::spawn(move || {
        if let Some(r) = se {
            pump(&a2, r);
        }
    });
    let status = child.wait().map_err(|e| e.to_string())?;
    let _ = t1.join();
    let _ = t2.join();
    if status.success() {
        Ok(())
    } else {
        Err(format!("spotDL stopped with code {} (see the log)", status.code().unwrap_or(-1)))
    }
}

// visible: a real terminal window (via a tiny .bat), we wait until it closes
fn run_visible(tmp: &Path, bin: &Path, out: &Path, url: &str) -> Result<(), String> {
    let exit_file = tmp.join("spotdl_exit.txt");
    let _ = fs::remove_file(&exit_file);
    let bat = tmp.join("run_spotdl.bat");
    let p = |x: &Path| x.display().to_string().replace('%', "%%");
    let script = format!(
        "@echo off\r\nchcp 65001 >nul\r\ntitle VOID - spotDL\r\nset \"PATH={bin};%PATH%\"\r\ncd /d \"{out}\"\r\n\"{sp}\" download \"{url}\" --ffmpeg \"{ff}\" --output \"{tpl}\" --threads 8\r\n>\"{exit}\" echo %errorlevel%\r\necho.\r\necho Finished. This window closes in 5 seconds.\r\ntimeout /t 5 >nul\r\n",
        bin = p(bin),
        out = p(out),
        sp = p(&bin.join("spotdl.exe")),
        url = url.replace('%', "%%"),
        ff = p(&bin.join("ffmpeg.exe")),
        tpl = output_template(url),
        exit = p(&exit_file),
    );
    fs::write(&bat, script).map_err(|e| e.to_string())?;

    let mut c = Command::new("cmd");
    c.stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null());
    #[cfg(windows)]
    {
        c.raw_arg(format!("/C start \"VOID spotDL\" /wait \"{}\"", bat.display()));
        c.creation_flags(CREATE_NO_WINDOW);
    }
    c.status().map_err(|e| format!("Couldn't open the terminal: {e}"))?;

    match fs::read_to_string(&exit_file) {
        Ok(s) if s.trim() == "0" => Ok(()),
        Ok(s) => Err(format!("spotDL stopped with code {}", s.trim())),
        Err(_) => Err("The terminal was closed before spotDL finished".into()),
    }
}

// returns the songs this download added, so the page can add them without a full rescan
#[tauri::command]
async fn spotdl_download(
    app: AppHandle,
    url: String,
    folder: String,
    visible: bool,
) -> Result<Vec<Item>, String> {
    let url = url.trim().to_string();
    if !valid_url(&url) {
        return Err("Type a song name or paste a link".into());
    }
    let bin = ensure_tools(&app).await?;
    let out = PathBuf::from(&folder);
    fs::create_dir_all(&out).map_err(|e| format!("Can't use that folder: {e}"))?;
    let tmp = data_dir(&app)?;
    say(&app, "status", "Downloading with spotDL\u{2026}", None);
    let app2 = app.clone();
    tauri::async_runtime::spawn_blocking(move || -> Result<Vec<Item>, String> {
        let before: HashSet<String> = audio_items(&out).into_iter().map(|i| i.path).collect();
        if visible {
            run_visible(&tmp, &bin, &out, &url)?;
        } else {
            run_hidden(&app2, &bin, &out, &url)?;
        }
        Ok(audio_items(&out)
            .into_iter()
            .filter(|i| !before.contains(&i.path))
            .collect::<Vec<Item>>())
    })
    .await
    .map_err(|e| e.to_string())?
}

// =====================================================================
//  library helpers: playlists (= folders), import, liked songs
// =====================================================================
fn item_for(root: &Path, p: &Path) -> Item {
    let rname = root
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_else(|| "Music".into());
    let rel = format!(
        "{}/{}",
        rname,
        p.strip_prefix(root).unwrap_or(p).to_string_lossy().replace('\\', "/")
    );
    Item {
        path: p.to_string_lossy().into_owned(),
        rel,
        size: fs::metadata(p).map(|m| m.len()).unwrap_or(0),
    }
}

// names of the folders directly inside the music folder (so empty playlists show up too)
#[tauri::command]
fn list_dirs(path: String) -> Vec<String> {
    let mut v: Vec<String> = fs::read_dir(&path)
        .map(|rd| {
            rd.flatten()
                .filter(|e| e.path().is_dir())
                .map(|e| e.file_name().to_string_lossy().into_owned())
                .collect()
        })
        .unwrap_or_default();
    v.sort();
    v
}

#[tauri::command]
fn make_dir(path: String) -> Result<(), String> {
    fs::create_dir_all(&path).map_err(|e| e.to_string())
}

// copy song files into a folder (skips files that are already there)
#[tauri::command]
async fn copy_into(paths: Vec<String>, dest: String, root: String) -> Result<Vec<Item>, String> {
    let d = PathBuf::from(&dest);
    let r = PathBuf::from(&root);
    fs::create_dir_all(&d).map_err(|e| e.to_string())?;
    let mut out = vec![];
    for s in paths {
        let src = PathBuf::from(&s);
        let Some(name) = src.file_name() else { continue };
        let target = d.join(name);
        if !target.exists() {
            fs::copy(&src, &target).map_err(|e| format!("{}: {e}", src.display()))?;
        }
        out.push(item_for(&r, &target));
    }
    Ok(out)
}

// use a folder as a playlist: songs from outside the music folder are copied in
#[tauri::command]
async fn import_folder(src: String, root: String) -> Result<Vec<Item>, String> {
    let s = PathBuf::from(&src);
    let r = PathBuf::from(&root);
    if !s.is_dir() {
        return Err("not a folder".into());
    }
    let inside = s.starts_with(&r);
    let name = s.file_name().map(|n| n.to_os_string()).ok_or("bad folder")?;
    let dest = r.join(&name);
    let mut out = vec![];
    for it in audio_items(&s) {
        let p = PathBuf::from(&it.path);
        if inside {
            out.push(item_for(&r, &p));
            continue;
        }
        let Some(fname) = p.file_name() else { continue };
        fs::create_dir_all(&dest).map_err(|e| e.to_string())?;
        let t = dest.join(fname);
        if !t.exists() {
            fs::copy(&p, &t).map_err(|e| e.to_string())?;
        }
        out.push(item_for(&r, &t));
    }
    if !inside {
        fs::create_dir_all(&dest).map_err(|e| e.to_string())?;
    }
    Ok(out)
}

// un-like: only files inside a "Liked Songs" folder can be deleted
#[tauri::command]
fn delete_liked(path: String) -> Result<(), String> {
    let p = PathBuf::from(&path);
    let ok = p
        .parent()
        .and_then(|d| d.file_name())
        .map(|n| n == "Liked Songs")
        .unwrap_or(false);
    if !ok {
        return Err("Only files inside the Liked Songs folder can be removed".into());
    }
    fs::remove_file(&p).map_err(|e| e.to_string())
}

// =====================================================================
//  VOID home folder:  C:\VOID   (Music, Logs, README)
//  - Music : the default music library (downloads and imports land here)
//  - Logs  : void.log keeps errors and notable events
//  - README: how to use VOID + the legal things to know
// =====================================================================
const README_TXT: &str = include_str!("../assets/README.txt");
const LICENSE_TXT: &str = include_str!("../assets/LICENSE.txt");
const NOTICES_TXT: &str = include_str!("../assets/THIRD-PARTY-NOTICES.txt");

static HOME: OnceLock<PathBuf> = OnceLock::new();
static WAS_MINIMIZED: AtomicBool = AtomicBool::new(false);

fn void_home() -> &'static PathBuf {
    HOME.get_or_init(|| {
        let drive = std::env::var("SystemDrive").unwrap_or_else(|_| "C:".into());
        let p = PathBuf::from(format!("{}\\VOID", drive));
        if fs::create_dir_all(&p).is_ok() {
            return p;
        }
        // C:\ not writable -> use the user's profile instead
        let q = PathBuf::from(std::env::var("USERPROFILE").unwrap_or_else(|_| ".".into())).join("VOID");
        let _ = fs::create_dir_all(&q);
        q
    })
}

// "YYYY-MM-DD HH:MM:SS" (UTC) without extra crates
fn timestamp() -> String {
    let secs = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0) as i64;
    let days = secs.div_euclid(86400);
    let rem = secs.rem_euclid(86400);
    let z = days + 719468;
    let era = z.div_euclid(146097);
    let doe = z.rem_euclid(146097);
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let mth = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = yoe + era * 400 + if mth <= 2 { 1 } else { 0 };
    format!(
        "{:04}-{:02}-{:02} {:02}:{:02}:{:02}",
        y, mth, d, rem / 3600, (rem % 3600) / 60, rem % 60
    )
}

fn log_line(level: &str, msg: &str) {
    let dir = void_home().join("Logs");
    let _ = fs::create_dir_all(&dir);
    let file = dir.join("void.log");
    if fs::metadata(&file).map(|m| m.len() > 1_000_000).unwrap_or(false) {
        let _ = fs::rename(&file, dir.join("void.old.log")); // keep the log from growing forever
    }
    if let Ok(mut f) = fs::OpenOptions::new().create(true).append(true).open(&file) {
        let _ = writeln!(f, "{} [{}] {}", timestamp(), level, msg.replace('\n', " | "));
    }
}

fn write_if_changed(path: &Path, text: &str) {
    if fs::read_to_string(path).map(|old| old == text).unwrap_or(false) {
        return;
    }
    let _ = fs::write(path, text);
}

fn init_home() {
    let h = void_home();
    for d in ["Music", "Logs", "README"] {
        let _ = fs::create_dir_all(h.join(d));
    }
    let r = h.join("README");
    write_if_changed(&r.join("README.txt"), README_TXT);
    write_if_changed(&r.join("LICENSE.txt"), LICENSE_TXT);
    write_if_changed(&r.join("THIRD-PARTY-NOTICES.txt"), NOTICES_TXT);
}

// the default music folder (C:\VOID\Music)
#[tauri::command]
fn void_music() -> Result<String, String> {
    let m = void_home().join("Music");
    fs::create_dir_all(&m).map_err(|e| e.to_string())?;
    Ok(m.to_string_lossy().into_owned())
}

// the page reports its errors here -> C:\VOID\Logs\void.log
#[tauri::command]
fn log_event(level: String, msg: String) {
    let lv: String = level.chars().take(8).collect();
    let text: String = msg.chars().take(2000).collect();
    log_line(&lv, &text);
}

fn main() {
    init_home();
    std::panic::set_hook(Box::new(|info| log_line("PANIC", &info.to_string())));
    log_line("INFO", "VOID started");
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .on_window_event(|window, event| {
            // minimized / restored -> the page stops drawing while hidden (the sound keeps playing)
            if let tauri::WindowEvent::Resized(_) = event {
                let min = window.is_minimized().unwrap_or(false);
                if WAS_MINIMIZED.swap(min, Ordering::Relaxed) != min {
                    let _ = window.emit("void-minimized", min);
                }
            }
        })
        .invoke_handler(tauri::generate_handler![
            last_folder,
            scan,
            read_range,
            spotdl_setup,
            spotdl_tools_status,
            spotdl_download,
            list_dirs,
            make_dir,
            copy_into,
            import_folder,
            delete_liked,
            void_music,
            log_event
        ])
        .run(tauri::generate_context!())
        .expect("error while running VOID");
}
