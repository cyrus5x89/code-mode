//! cm: the Rust front of the code-mode runner (Cyrus 2026-11-25: faster and lighter than the all-Python cm).
//!
//! ~/.local/bin/cm links to rs/bin/cm (a copy of cargo's output outside target/, so cargo clean leaves it, made by `python3 cm.py --build`). This binary
//! parses the command line, finds the state folder (same key and place as cm.py: CM_SESSION or sha1 of the working
//! folder, in CM_DIR or $TMPDIR/code-mode), takes the lock, drops state older than CM_TTL hours, numbers the step,
//! saves the full output for `cm out`, cuts what is printed to CM_CAP chars and prints the footer. Only the step
//! script runs in Python: one worker per step (cm.worker(): it loads and saves the kept variables). While it runs, this
//! process serves the worker's helpers over two inherited pipes (CM_FDS; pipes, not a socket: Codex's sandbox refuses
//! AF_UNIX): shell jobs for sh / par / j / grep (bash -c in their own process group, the group killed at the timeout,
//! side by side for par), codemap and note. When the step ends, any job group still running is killed.
//!
//! When this build is older than its sources (or the arguments are not ours, e.g. --help) the call is handed to
//! `python3 cm.py` unchanged, and a stale build starts one background rebuild (at most every 10 minutes), so no
//! call ever waits for cargo.
//!
//! Commands: (none) | run | -  step from stdin;  -f FILE;  vars;  out [N] [a:b];  reset.
//! Requests: "ID LEN\n" + LEN bytes of netstrings; answers "ID LEN\n" + LEN bytes, in any order (see answer()):
//!   jobs WORKERS MERGE ENV (TIMEOUT CWD HAS_INPUT INPUT CMD)...  -> per job "code outlen errlen flag\n" out err
//!   note PATH FACT...                                            -> "0\n" or "1\n" + why
//!   codemap ROOT OUT PER_FILE EXT...                             -> "0 files\n" or "1\n" + why

use std::ffi::OsString;
use std::fs::{self, File, OpenOptions};
use std::io::{self, BufRead, Read, Write};
use std::ffi::OsStr;
use std::os::unix::ffi::OsStrExt;
use std::os::fd::AsRawFd;
use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::path::{Component, Path, PathBuf};
use std::process::{Command, ExitCode, Stdio};
use std::sync::atomic::{AtomicI32, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex, mpsc};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const SKIP_DIRS: [&str; 10] = [".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".next", "target", ".cache"];

#[repr(C)]
struct Tm {
    sec: i32,
    min: i32,
    hour: i32,
    mday: i32,
    mon: i32,
    year: i32,
    wday: i32,
    yday: i32,
    isdst: i32,
    gmtoff: i64,
    zone: *const u8,
}

unsafe extern "C" {
    fn kill(pid: i32, sig: i32) -> i32;
    fn signal(sig: i32, handler: extern "C" fn(i32)) -> usize;
    fn localtime_r(t: *const i64, out: *mut Tm) -> *mut Tm;
    fn fcntl(fd: i32, cmd: i32, ...) -> i32;
}

fn main() -> ExitCode {
    let raw: Vec<OsString> = std::env::args_os().skip(1).collect();
    let a: Vec<String> = raw.iter().map(|s| s.to_string_lossy().into_owned()).collect();
    ExitCode::from(front(&raw, &a))
}

// ------------------------------------------------------------------ front door

fn front(raw: &[OsString], a: &[String]) -> u8 {
    let exe = std::env::current_exe().and_then(fs::canonicalize).unwrap_or_default();
    let krate = exe
        .ancestors()
        .skip(1)
        .take(4)
        .find(|d| d.join("Cargo.toml").is_file() && d.join("src/main.rs").is_file())
        .map(Path::to_path_buf)
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_MANIFEST_DIR")));
    let cm_py = krate.parent().unwrap_or(&krate).join("skills/code-mode/scripts/cm.py");
    let ours = match a.first().map(String::as_str) {
        None | Some("run" | "-" | "reset" | "vars" | "out") => true,
        Some("-f") => a.len() > 1,
        _ => false,
    };
    let fresh = fresh(&krate);
    if !fresh {
        build_in_background(&krate, &cm_py);
    }
    if !fresh || !ours {
        let e = Command::new("python3").arg(&cm_py).args(raw).exec();
        println!("cm: python3 {} did not start: {e}", cm_py.display());
        return 1;
    }
    let d = state_dir();
    match a.first().map(String::as_str) {
        Some("reset") => {
            let _ = fs::remove_dir_all(&d);
            println!("cm: state cleared");
            0
        }
        Some("vars") => vars(&d),
        Some("out") => out(&d, &a[1..]),
        Some("-f") => match fs::read(&a[1]) {
            Ok(src) => step(&d, src, &cm_py),
            Err(e) => {
                println!("cm: cannot read {}: {e}", a[1]);
                1
            }
        },
        _ => {
            let mut src = Vec::new();
            let _ = io::stdin().read_to_end(&mut src);
            step(&d, src, &cm_py)
        }
    }
}

fn mtime(p: &Path) -> Option<SystemTime> {
    fs::metadata(p).and_then(|m| m.modified()).ok()
}

/// rs/bin/cm was copied by a build that started after every source file last changed (cm.py --build sets the
/// stamp to the build's start). No sources beside the binary: nothing to compare, so fresh.
fn fresh(krate: &Path) -> bool {
    if !krate.join("Cargo.toml").is_file() {
        return true;
    }
    let Some(built) = mtime(&krate.join("bin/cm.built")) else { return false };
    let mut srcs = vec![krate.join("Cargo.toml"), krate.join("Cargo.lock")];
    if let Ok(rd) = fs::read_dir(krate.join("src")) {
        srcs.extend(rd.flatten().map(|e| e.path()));
    }
    srcs.iter().filter_map(|p| mtime(p)).all(|t| t <= built)
}

fn build_in_background(krate: &Path, cm_py: &Path) {
    let log = krate.join("bin/background-build.log");
    if mtime(&log).and_then(|t| t.elapsed().ok()).is_some_and(|e| e.as_secs() < 600) {
        return;
    }
    let _ = fs::create_dir_all(krate.join("bin"));
    let Ok(f) = File::create(&log) else { return };
    let Ok(f2) = f.try_clone() else { return };
    let _ = Command::new("python3").arg(cm_py).arg("--build").stdin(Stdio::null()).stdout(f).stderr(f2).process_group(0).spawn();
}

// ------------------------------------------------------------------ state folder (same place as cm.py state_dir)

fn state_dir() -> PathBuf {
    let key = match std::env::var_os("CM_SESSION").filter(|s| !s.is_empty()) {
        Some(s) => s.to_string_lossy().into_owned(),
        None => sha1_hex(std::env::current_dir().unwrap_or_default().as_os_str().as_bytes())[..12].to_string(),
    };
    let base = match std::env::var_os("CM_DIR").filter(|s| !s.is_empty()) {
        Some(b) => abspath(Path::new(&b)),
        None => tempdir().join("code-mode"),
    };
    let d = base.join(&key);
    if fs::create_dir_all(d.join("out")).is_ok() {
        return d;
    }
    let d = abspath(&Path::new(".cm").join(&key)); // a sandbox that only lets us write the working directory
    let _ = fs::create_dir_all(d.join("out"));
    d
}

/// Python's tempfile.gettempdir(): the first of TMPDIR, TEMP, TMP, /tmp, /var/tmp, /usr/tmp, the working folder where
/// a file can be made, absolute and normal (so cm.py, which uses it, picks the same state folder).
fn tempdir() -> PathBuf {
    let env = ["TMPDIR", "TEMP", "TMP"].iter().filter_map(|k| std::env::var_os(k).filter(|v| !v.is_empty())).map(PathBuf::from);
    let fixed = ["/tmp", "/var/tmp", "/usr/tmp"].iter().map(PathBuf::from);
    let cwd = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
    for dir in env.chain(fixed).chain([cwd.clone()]) {
        let dir = abspath(&dir);
        let probe = dir.join(format!(".cm-probe-{}", std::process::id()));
        if OpenOptions::new().write(true).create_new(true).open(&probe).is_ok() {
            let _ = fs::remove_file(&probe);
            return dir;
        }
    }
    cwd
}

/// os.path.abspath: joined to the working folder, "." and ".." folded, no trailing slash.
fn abspath(p: &Path) -> PathBuf {
    let p = if p.is_absolute() { p.to_path_buf() } else { std::env::current_dir().unwrap_or_default().join(p) };
    let mut out = PathBuf::new();
    for c in p.components() {
        match c {
            Component::ParentDir => {
                out.pop();
            }
            Component::CurDir => {}
            c => out.push(c),
        }
    }
    out
}

fn sha1_hex(data: &[u8]) -> String {
    let mut h: [u32; 5] = [0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0];
    let mut m = data.to_vec();
    m.push(0x80);
    while m.len() % 64 != 56 {
        m.push(0);
    }
    m.extend_from_slice(&((data.len() as u64) * 8).to_be_bytes());
    for chunk in m.chunks(64) {
        let mut w = [0u32; 80];
        for i in 0..16 {
            w[i] = u32::from_be_bytes([chunk[4 * i], chunk[4 * i + 1], chunk[4 * i + 2], chunk[4 * i + 3]]);
        }
        for i in 16..80 {
            w[i] = (w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16]).rotate_left(1);
        }
        let [mut a, mut b, mut c, mut d, mut e] = h;
        for (i, wi) in w.iter().enumerate() {
            let (f, k) = match i {
                0..=19 => ((b & c) | (!b & d), 0x5A827999),
                20..=39 => (b ^ c ^ d, 0x6ED9EBA1),
                40..=59 => ((b & c) | (b & d) | (c & d), 0x8F1BBCDC),
                _ => (b ^ c ^ d, 0xCA62C1D6),
            };
            let t = a.rotate_left(5).wrapping_add(f).wrapping_add(e).wrapping_add(k).wrapping_add(*wi);
            e = d;
            d = c;
            c = b.rotate_left(30);
            b = a;
            a = t;
        }
        for (x, y) in h.iter_mut().zip([a, b, c, d, e]) {
            *x = x.wrapping_add(y);
        }
    }
    h.iter().map(|x| format!("{x:08x}")).collect()
}

/// The newest of step / state.pkl older than CM_TTL hours (default 6): drop the kept state and the saved outputs.
fn expire(d: &Path) {
    let ttl = std::env::var("CM_TTL").ok().and_then(|s| s.trim().parse::<f64>().ok()).unwrap_or(6.0) * 3600.0;
    let newest = ["step", "state.pkl"].iter().filter_map(|f| mtime(&d.join(f))).max();
    if newest.and_then(|t| t.elapsed().ok()).is_some_and(|e| e.as_secs_f64() > ttl) {
        if let Ok(rd) = fs::read_dir(d.join("out")) {
            for e in rd.flatten() {
                if e.path().extension().is_some_and(|x| x == "txt") {
                    let _ = fs::remove_file(e.path());
                }
            }
        }
        for f in ["state.pkl", "vars.txt", "step"] {
            let _ = fs::remove_file(d.join(f));
        }
    }
}

/// The step file; state from before it existed (step inside state.pkl): the highest saved output.
fn last_step(d: &Path) -> u64 {
    if let Ok(s) = fs::read_to_string(d.join("step")) {
        return s.trim().parse().unwrap_or(0);
    }
    fs::read_dir(d.join("out"))
        .map(|rd| rd.flatten().filter_map(|e| e.file_name().to_str()?.strip_suffix(".txt")?.parse().ok()).max().unwrap_or(0))
        .unwrap_or(0)
}

fn cap_n() -> usize {
    std::env::var("CM_CAP").ok().and_then(|s| s.trim().parse().ok()).unwrap_or(6000)
}

/// cm.py _cap: head 60 % + tail 35 % of n chars, with a marker saying how much went.
fn cap(s: &str, n: usize) -> String {
    let len = s.chars().count();
    if len <= n {
        return s.to_string();
    }
    let (h, t) = ((n as f64 * 0.6) as usize, (n as f64 * 0.35) as usize);
    let at = |k: usize| s.char_indices().nth(k).map_or(s.len(), |(i, _)| i);
    format!("{}\n... [{} chars cut; slice the full text: cm out [step] a:b] ...\n{}", &s[..at(h)], len - h - t, &s[at(len - t)..])
}

/// Python's str.splitlines().
fn splitlines(s: &str) -> Vec<&str> {
    let mut out = Vec::new();
    let mut start = 0;
    let mut it = s.char_indices().peekable();
    while let Some((i, c)) = it.next() {
        if matches!(c, '\n' | '\r' | '\x0b' | '\x0c' | '\x1c' | '\x1d' | '\x1e' | '\u{85}' | '\u{2028}' | '\u{2029}') {
            out.push(&s[start..i]);
            start = i + c.len_utf8();
            if c == '\r' && it.peek().is_some_and(|&(_, n)| n == '\n') {
                it.next();
                start += 1;
            }
        }
    }
    if start < s.len() {
        out.push(&s[start..]);
    }
    out
}

/// Lines the way Python's universal newlines see them (\r\n, \r, \n), as ast and file iteration count them.
fn uni_lines(s: &str) -> Vec<&str> {
    let mut out = Vec::new();
    let b = s.as_bytes();
    let (mut start, mut i) = (0, 0);
    while i < b.len() {
        if b[i] == b'\n' || b[i] == b'\r' {
            out.push(&s[start..i]);
            i += if b[i] == b'\r' && b.get(i + 1) == Some(&b'\n') { 2 } else { 1 };
            start = i;
        } else {
            i += 1;
        }
    }
    if start < b.len() {
        out.push(&s[start..]);
    }
    out
}

// ------------------------------------------------------------------ vars, out

fn vars(d: &Path) -> u8 {
    expire(d);
    let rows = fs::read_to_string(d.join("vars.txt")).unwrap_or_default();
    let mut o = String::new();
    let mut k = 0;
    for line in rows.lines().filter(|l| !l.is_empty()) {
        let mut it = line.splitn(3, '\t');
        let (name, ty, size) = (it.next().unwrap_or(""), it.next().unwrap_or(""), it.next().unwrap_or(""));
        o.push_str(&format!("{name}: {ty} {size}\n"));
        k += 1;
    }
    o.push_str(&format!("[{k} kept after step {}]\n", last_step(d)));
    let _ = io::stdout().write_all(o.as_bytes());
    0
}

/// Python slice bounds of lines[a:b] for a list of len n.
fn py_slice(n: usize, a: i64, b: Option<i64>) -> (usize, usize) {
    let norm = |x: i64| -> usize {
        if x < 0 { (x + n as i64).max(0) as usize } else { (x as usize).min(n) }
    };
    let (s, e) = (norm(a), b.map_or(n, norm));
    (s, e.max(s))
}

fn out(d: &Path, rest: &[String]) -> u8 {
    expire(d);
    let mut rest = rest;
    let n = match rest.first() {
        Some(s) if !s.is_empty() && s.bytes().all(|c| c.is_ascii_digit()) => {
            rest = &rest[1..];
            s.parse().unwrap_or(u64::MAX)
        }
        _ => last_step(d),
    };
    let Ok(raw) = fs::read(d.join("out").join(format!("{n}.txt"))) else {
        println!("cm: no output for step {n}");
        return 1;
    };
    let text = String::from_utf8_lossy(&raw);
    let lines = splitlines(&text);
    let o = match rest.first().filter(|s| s.contains(':')) {
        Some(r) => {
            // a range is printed whole: asking for it is the point
            let (a, b) = r.split_once(':').unwrap_or((r, ""));
            let num = |x: &str| if x.is_empty() { Ok(None) } else { x.trim().parse::<i64>().map(Some) };
            let (Ok(a), Ok(b)) = (num(a), num(b)) else {
                println!("cm: bad range {r} (want a:b, e.g. 1:40)");
                return 2;
            };
            let (s, e) = py_slice(lines.len(), a.filter(|&x| x != 0).unwrap_or(1) - 1, b);
            lines[s..e].join("\n")
        }
        None => cap(&lines.join("\n"), cap_n()),
    };
    let _ = io::stdout().write_all((o + "\n").as_bytes());
    0
}

// ------------------------------------------------------------------ one step

static WORKER: AtomicI32 = AtomicI32::new(0);

/// SIGTERM / SIGHUP to cm go on to the Python worker; cm then prints what the worker managed to say. SIGINT is not
/// passed on: Ctrl-C reaches the worker from the terminal (same process group), and a second one could hit Python
/// while it handles the first and lose the step; cm only stays alive for it (a handler, not SIG_IGN, which the worker
/// would inherit).
extern "C" fn forward(sig: i32) {
    if sig == 2 {
        return;
    }
    let pid = WORKER.load(Ordering::SeqCst);
    if pid > 0 {
        unsafe {
            kill(pid, sig);
        }
    }
}

fn step(d: &Path, src: Vec<u8>, cm_py: &Path) -> u8 {
    if String::from_utf8_lossy(&src).trim().is_empty() {
        println!("cm: empty script (pass it on stdin: cm <<'PY' ... PY)");
        return 2;
    }
    let lock = match File::create(d.join("lock")) {
        Ok(f) => f,
        Err(e) => {
            println!("cm: cannot open the lock in {}: {e}", d.display());
            return 1;
        }
    };
    let _ = lock.lock(); // two cm calls in one folder take turns instead of losing state
    expire(d);
    let n = last_step(d) + 1;
    let t0 = Instant::now();
    let (mut text, status) = run_worker(d, src, cm_py);
    let secs = t0.elapsed().as_secs_f64();
    let err = match status {
        Ok(0) => false,
        Ok(1) => true,
        Ok(c) => {
            text.push_str(&format!("\n[cm: python3 ended with exit code {c}]\n"));
            true
        }
        Err(m) => {
            text.push_str(&format!("\n[cm: {m}]\n"));
            true
        }
    };
    let _ = fs::create_dir_all(d.join("out"));
    let _ = fs::write(d.join("out").join(format!("{n}.txt")), &text);
    let _ = fs::write(d.join("step"), n.to_string());
    let mut names: Vec<String> = fs::read_to_string(d.join("vars.txt"))
        .unwrap_or_default()
        .lines()
        .filter_map(|l| l.split('\t').next().filter(|s| !s.is_empty()).map(str::to_string))
        .collect();
    names.sort();
    let mut kept = names.iter().take(12).cloned().collect::<Vec<_>>().join(", ");
    if names.len() > 12 {
        kept.push_str(&format!(" (+{})", names.len() - 12));
    }
    let mut o = String::new();
    if !text.trim().is_empty() {
        o.push_str(&cap(text.trim_end_matches('\n'), cap_n()));
        o.push('\n');
    }
    o.push_str(&format!(
        "[cm #{n} {}{secs:.1}s | {} chars{}]\n",
        if err { "ERROR " } else { "" },
        text.chars().count(),
        if kept.is_empty() { String::new() } else { format!(" | kept: {kept}") }
    ));
    let _ = io::stdout().write_all(o.as_bytes());
    drop(lock);
    u8::from(err)
}

/// python3 -c 'import cm; cm.worker()' (cached bytecode, sys.path[0] = the scripts folder as when cm.py runs), the
/// script on stdin, stdout + stderr on one pipe. -> (its text, exit code or why it has none)
fn run_worker(d: &Path, src: Vec<u8>, cm_py: &Path) -> (String, Result<i32, String>) {
    let dir = cm_py.parent().unwrap_or(Path::new("."));
    let (mut r, w) = match io::pipe() {
        Ok(p) => p,
        Err(e) => return (String::new(), Err(format!("no pipe: {e}"))),
    };
    let w2 = match w.try_clone() {
        Ok(x) => x,
        Err(e) => return (String::new(), Err(format!("no pipe: {e}"))),
    };
    let mut cmd = Command::new("python3");
    cmd.env_remove("CM_FDS"); // never an outer step's pipes
    // requests worker -> us, answers us -> worker; the worker's two ends stay open across exec (no pipes: Python helpers)
    let chan = io::pipe().and_then(|(qr, qw)| io::pipe().map(|(ar, aw)| (qr, qw, ar, aw))).ok();
    let mut child_ends = None;
    if let Some((qr, qw, ar, aw)) = chan {
        unsafe {
            fcntl(qw.as_raw_fd(), 2, 0); // F_SETFD: clear FD_CLOEXEC
            fcntl(ar.as_raw_fd(), 2, 0);
        }
        cmd.env("CM_FDS", format!("{},{}", ar.as_raw_fd(), qw.as_raw_fd()));
        child_ends = Some((qw, ar));
        thread::spawn(move || serve(qr, aw));
    }
    cmd.arg("-c")
        .arg("import sys; sys.path[0] = sys.argv[1]; import cm; sys.exit(cm.worker())")
        .arg(dir)
        .env("CM_STATE_DIR", d)
        .stdin(Stdio::piped())
        .stdout(w)
        .stderr(w2);
    for sig in [1, 2, 15] {
        unsafe {
            signal(sig, forward);
        }
    }
    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => return (String::new(), Err(format!("python3 did not start: {e}"))),
    };
    drop(cmd); // our ends of the output pipe: the reader sees EOF when the worker (and its children) close theirs
    drop(child_ends);
    WORKER.store(child.id() as i32, Ordering::SeqCst);
    let feed = child.stdin.take().map(|mut s| thread::spawn(move || drop(s.write_all(&src))));
    let buf = Arc::new(Mutex::new(Vec::new()));
    let (tx, rx) = mpsc::channel();
    let b2 = Arc::clone(&buf);
    thread::spawn(move || {
        let mut chunk = [0u8; 65536];
        loop {
            match r.read(&mut chunk) {
                Ok(0) => break,
                Ok(k) => b2.lock().unwrap().extend_from_slice(&chunk[..k]),
                Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
                Err(_) => break,
            }
        }
        let _ = tx.send(());
    });
    let status = child.wait();
    WORKER.store(0, Ordering::SeqCst);
    for pid in LIVE.lock().unwrap().drain(..) {
        unsafe {
            kill(-pid, 9); // a job the dead worker no longer waits for
        }
    }
    // a process the script left running in the background may hold the pipe: don't wait for it
    let _ = rx.recv_timeout(Duration::from_millis(300));
    if let Some(t) = feed {
        let _ = t.join();
    }
    let text = String::from_utf8_lossy(&buf.lock().unwrap()).into_owned();
    let res = match status {
        Ok(s) => s.code().ok_or_else(|| format!("python3 killed by signal {}", s.signal().unwrap_or(0))),
        Err(e) => Err(format!("lost the worker: {e}")),
    };
    (text, res)
}

// ------------------------------------------------------------------ the worker's helpers, over the pipes

static LIVE: Mutex<Vec<i32>> = Mutex::new(Vec::new()); // process groups of the jobs still running

struct Job {
    timeout: f64,
    cwd: Option<String>,
    input: Option<Vec<u8>>,
    cmd: String,
}

struct Done {
    code: i32,
    out: Vec<u8>,
    err: Vec<u8>,
    flag: u8, // 0 ran, 1 killed at the timeout, 2 did not start (code = errno, out = why)
}

fn netstrings(b: &[u8]) -> Option<Vec<&[u8]>> {
    let mut out = Vec::new();
    let mut i = 0;
    while i < b.len() {
        let c = i + b[i..].iter().position(|&x| x == b':')?;
        let n: usize = std::str::from_utf8(&b[i..c]).ok()?.parse().ok()?;
        let s = c + 1;
        out.push(b.get(s..s + n)?);
        i = s + n;
    }
    Some(out)
}

fn read_all(mut r: impl Read + Send + 'static) -> thread::JoinHandle<Vec<u8>> {
    thread::spawn(move || {
        let mut v = Vec::new();
        let _ = r.read_to_end(&mut v);
        v
    })
}

/// bash -c in its own process group with the worker's environment, so the timeout kills the whole pipeline (and what
/// it put in the background).
fn run_job(j: &Job, merge: bool, env: &[(&OsStr, &OsStr)]) -> Done {
    let fail = |e: io::Error| Done { code: e.raw_os_error().unwrap_or(0), out: e.to_string().into_bytes(), err: Vec::new(), flag: 2 };
    let mut c = Command::new("bash");
    c.arg("-c").arg(&j.cmd).process_group(0).env_clear().envs(env.iter().copied());
    if let Some(d) = &j.cwd {
        c.current_dir(d);
    }
    c.stdin(if j.input.is_some() { Stdio::piped() } else { Stdio::null() });
    let (out_r, out_w) = match io::pipe() {
        Ok(p) => p,
        Err(e) => return fail(e),
    };
    let mut err_r = None;
    if merge {
        match out_w.try_clone() {
            Ok(w2) => c.stdout(out_w).stderr(w2),
            Err(e) => return fail(e),
        };
    } else {
        match io::pipe() {
            Ok((er, ew)) => {
                err_r = Some(er);
                c.stdout(out_w).stderr(ew)
            }
            Err(e) => return fail(e),
        };
    }
    let mut child = match c.spawn() {
        Ok(ch) => ch,
        Err(e) => return fail(e),
    };
    drop(c);
    let pid = child.id() as i32;
    LIVE.lock().unwrap().push(pid);
    let feed = match (child.stdin.take(), j.input.clone()) {
        (Some(mut s), Some(inp)) => Some(thread::spawn(move || drop(s.write_all(&inp)))),
        _ => None,
    };
    let ro = read_all(out_r);
    let re = err_r.map(read_all);
    let (tx, rx) = mpsc::channel::<()>();
    let secs = if j.timeout > 0.0 && j.timeout.is_finite() { j.timeout.min(1e9) } else { 1e9 };
    let killer = thread::spawn(move || {
        if rx.recv_timeout(Duration::from_secs_f64(secs)) == Err(mpsc::RecvTimeoutError::Timeout) {
            unsafe {
                kill(-pid, 9);
            }
            return true;
        }
        false
    });
    let status = child.wait();
    let out = ro.join().unwrap_or_default();
    let err = re.map(|h| h.join().unwrap_or_default()).unwrap_or_default();
    let _ = tx.send(());
    let timed_out = killer.join().unwrap_or(false);
    if let Some(f) = feed {
        let _ = f.join();
    }
    LIVE.lock().unwrap().retain(|&p| p != pid);
    let code = match status {
        _ if timed_out => 124,
        Ok(s) => s.code().unwrap_or_else(|| -s.signal().unwrap_or(0)),
        Err(_) => -1,
    };
    Done { code, out, err, flag: u8::from(timed_out) }
}

/// jobs WORKERS MERGE ENV then five fields per job; up to WORKERS at once, answers in job order.
fn jobs(f: &[&[u8]]) -> Vec<u8> {
    if f.len() < 3 || (f.len() - 3) % 5 != 0 {
        return b"bad request".to_vec();
    }
    let s = |b: &[u8]| String::from_utf8_lossy(b).into_owned();
    let workers: usize = s(f[0]).parse().unwrap_or(4).max(1);
    let merge = f[1] != b"0";
    let env: Vec<(&OsStr, &OsStr)> = f[2]
        .split(|&c| c == 0)
        .filter_map(|kv| {
            let i = kv.iter().position(|&c| c == b'=')?;
            Some((OsStr::from_bytes(&kv[..i]), OsStr::from_bytes(&kv[i + 1..])))
        })
        .collect();
    let list: Vec<Job> = f[3..]
        .chunks(5)
        .map(|x| Job {
            timeout: s(x[0]).trim().parse().unwrap_or(300.0),
            cwd: Some(s(x[1])).filter(|c| !c.is_empty()),
            input: (x[2] == b"1").then(|| x[3].to_vec()),
            cmd: s(x[4]),
        })
        .collect();
    let done: Mutex<Vec<Option<Done>>> = Mutex::new((0..list.len()).map(|_| None).collect());
    let next = AtomicUsize::new(0);
    thread::scope(|sc| {
        for _ in 0..workers.min(list.len()) {
            sc.spawn(|| loop {
                let i = next.fetch_add(1, Ordering::SeqCst);
                let Some(j) = list.get(i) else { break };
                let r = run_job(j, merge, &env);
                done.lock().unwrap()[i] = Some(r);
            });
        }
    });
    let mut o = Vec::new();
    for r in done.into_inner().unwrap().into_iter().flatten() {
        o.extend_from_slice(format!("{} {} {} {}\n", r.code, r.out.len(), r.err.len(), r.flag).as_bytes());
        o.extend_from_slice(&r.out);
        o.extend_from_slice(&r.err);
    }
    o
}

/// Read framed requests until the worker closes its end; answer each in its own thread (a mixed par sends several).
fn serve(q: io::PipeReader, a: io::PipeWriter) {
    let a = Arc::new(Mutex::new(a));
    let mut q = io::BufReader::new(q);
    loop {
        let mut head = String::new();
        if q.read_line(&mut head).unwrap_or(0) == 0 {
            return;
        }
        let mut it = head.split_whitespace();
        let (Some(id), Some(n)) = (it.next().map(str::to_string), it.next().and_then(|s| s.parse::<usize>().ok())) else { return };
        let mut req = vec![0; n];
        if q.read_exact(&mut req).is_err() {
            return;
        }
        let a = Arc::clone(&a);
        thread::spawn(move || {
            let ans = answer(&req);
            let mut w = a.lock().unwrap();
            let _ = w.write_all(format!("{id} {}\n", ans.len()).as_bytes()).and_then(|_| w.write_all(&ans));
        });
    }
}

fn answer(req: &[u8]) -> Vec<u8> {
    let f = netstrings(req).unwrap_or_default();
    let s = |b: &[u8]| String::from_utf8_lossy(b).into_owned();
    match f.first().copied() {
        Some(b"jobs") => jobs(&f[1..]),
        Some(b"note") if f.len() > 1 => match note(Path::new(OsStr::from_bytes(f[1])), &f[2..].iter().map(|x| s(x)).collect::<Vec<_>>()) {
            Ok(()) => b"0\n".to_vec(),
            Err(e) => format!("1\n{e}").into_bytes(),
        },
        Some(b"codemap") if f.len() > 3 => {
            let exts: Vec<String> = f[4..].iter().map(|x| s(x)).collect();
            let per = s(f[3]).parse().unwrap_or(40);
            match codemap(Path::new(OsStr::from_bytes(f[1])), Path::new(OsStr::from_bytes(f[2])), per, &exts) {
                Ok(n) => format!("0 {n}\n").into_bytes(),
                Err(e) => format!("1\n{e}").into_bytes(),
            }
        }
        _ => b"bad request".to_vec(),
    }
}

/// Append facts, one line each, whitespace folded (cm.py note()).
fn note(p: &Path, facts: &[String]) -> io::Result<()> {
    if let Some(dir) = p.parent().filter(|x| !x.as_os_str().is_empty()) {
        fs::create_dir_all(dir)?;
    }
    let text: String = facts.iter().map(|f| format!("- {}\n", f.split_whitespace().collect::<Vec<_>>().join(" "))).collect();
    OpenOptions::new().append(true).create(true).open(p)?.write_all(text.as_bytes())
}

// ------------------------------------------------------------------ codemap

/// os.walk order: a folder's files (sorted), then its folders (sorted, skip list and .cm*, symlinks not followed).
fn walk(dir: &Path, rel: &str, f: &mut dyn FnMut(&Path, &str)) {
    let Ok(rd) = fs::read_dir(dir) else { return };
    let (mut dirs, mut files) = (Vec::new(), Vec::new());
    for e in rd.flatten() {
        let name = e.file_name().to_string_lossy().into_owned();
        let ft = e.file_type().ok();
        let link = ft.as_ref().is_some_and(|t| t.is_symlink());
        let is_dir = if link { fs::metadata(e.path()).is_ok_and(|m| m.is_dir()) } else { ft.is_some_and(|t| t.is_dir()) };
        if !is_dir {
            files.push(name);
        } else if !link && !SKIP_DIRS.contains(&name.as_str()) && !name.starts_with(".cm") {
            dirs.push(name);
        }
    }
    files.sort();
    dirs.sort();
    let join = |n: &str| if rel.is_empty() { n.to_string() } else { format!("{rel}/{n}") };
    for n in &files {
        f(&dir.join(n), &join(n));
    }
    for n in &dirs {
        walk(&dir.join(n), &join(n), f);
    }
}

/// "kw" followed by at least one blank -> the rest after the blanks.
fn kw<'a>(s: &'a str, k: &str) -> Option<&'a str> {
    let r = s.strip_prefix(k)?;
    let t = r.trim_start();
    (t.len() < r.len()).then_some(t)
}

fn ident(s: &str) -> Option<&str> {
    if !s.starts_with(|c: char| c.is_ascii_alphabetic() || c == '_') {
        return None;
    }
    let n = s.char_indices().find(|&(_, c)| !(c.is_alphanumeric() || c == '_')).map_or(s.len(), |(i, _)| i);
    Some(&s[..n])
}

/// cm.py _DEF for non-Python files: [export] [pub[(..)]] [async] def|fn|function|class|struct|enum|impl|trait NAME
fn def_name(line: &str) -> Option<&str> {
    let mut s = line.trim_start();
    if let Some(r) = kw(s, "export") {
        s = r;
    }
    if let Some(r) = s.strip_prefix("pub") {
        let paren = r.strip_prefix('(').and_then(|x| x.find(')').map(|i| &x[i + 1..])).and_then(|x| kw(x, ""));
        if let Some(t) = paren.or_else(|| kw(r, "")) {
            s = t;
        }
    }
    if let Some(r) = kw(s, "async") {
        s = r;
    }
    ["def", "fn", "function", "class", "struct", "enum", "impl", "trait"].iter().find_map(|k| kw(s, k).and_then(ident))
}

fn py_def(t: &str) -> Option<(bool, &str)> {
    if let Some(r) = kw(t, "class") {
        return ident(r).map(|n| (true, n));
    }
    kw(t, "def").or_else(|| kw(t, "async").and_then(|r| kw(r, "def"))).and_then(ident).map(|n| (false, n))
}

fn indent_of(line: &str) -> usize {
    let mut col = 0;
    for c in line.chars() {
        match c {
            ' ' => col += 1,
            '\t' => col = (col / 8 + 1) * 8,
            '\x0c' => col = 0,
            _ => break,
        }
    }
    col
}

/// Top-level def / class (and the methods of a class) with first-last lines, as ast gives them in cm.py: logical
/// lines from a small tokenizer (strings, brackets, comments, backslash), a block ends before the next logical line
/// that is not indented deeper.
fn py_outline(src: &str) -> Vec<String> {
    struct L<'a> {
        first: usize,
        last: usize,
        ind: usize,
        text: &'a str,
    }
    let lines = uni_lines(src);
    let mut logical: Vec<L> = Vec::new();
    let mut depth = 0i32;
    let mut string: Option<(u8, bool)> = None;
    let mut cur: Option<(usize, usize, &str)> = None;
    for (i, line) in lines.iter().enumerate() {
        if cur.is_none() {
            let t = line.trim_start_matches([' ', '\t', '\x0c']);
            if t.is_empty() || t.starts_with('#') {
                continue;
            }
            cur = Some((i + 1, indent_of(line), t));
        }
        let b = line.as_bytes();
        let (mut j, mut cont) = (0, false);
        while j < b.len() {
            let c = b[j];
            if let Some((q, triple)) = string {
                if c == b'\\' {
                    j += 2;
                    continue;
                }
                if c == q {
                    if !triple {
                        string = None;
                    } else if b[j..].starts_with(&[q, q, q]) {
                        string = None;
                        j += 3;
                        continue;
                    }
                }
                j += 1;
                continue;
            }
            match c {
                b'#' => break,
                b'\'' | b'"' => {
                    if b[j..].starts_with(&[c, c, c]) {
                        string = Some((c, true));
                        j += 3;
                        continue;
                    }
                    string = Some((c, false));
                }
                b'(' | b'[' | b'{' => depth += 1,
                b')' | b']' | b'}' => depth -= 1,
                b'\\' if j + 1 == b.len() => cont = true,
                _ => {}
            }
            j += 1;
        }
        if matches!(string, Some((_, false))) && !line.ends_with('\\') {
            string = None; // an unclosed one-line string is a syntax error: do not let it swallow the file
        }
        if string.is_none() && depth <= 0 && !cont {
            if let Some((first, ind, text)) = cur.take() {
                logical.push(L { first, last: i + 1, ind, text });
            }
            depth = 0;
        }
    }
    if let Some((first, ind, text)) = cur {
        logical.push(L { first, last: lines.len(), ind, text });
    }
    let end = |k: usize| {
        let mut j = k + 1;
        while j < logical.len() && logical[j].ind > logical[k].ind {
            j += 1;
        }
        j
    };
    let mut items = Vec::new();
    for k in 0..logical.len() {
        if logical[k].ind != 0 {
            continue;
        }
        let Some((class, name)) = py_def(logical[k].text) else { continue };
        let stop = end(k);
        items.push(format!("{name} {}-{}", logical[k].first, logical[stop - 1].last));
        if class && stop > k + 1 {
            let body = logical[k + 1].ind;
            for m in k + 1..stop {
                if logical[m].ind == body {
                    if let Some((false, meth)) = py_def(logical[m].text) {
                        items.push(format!("{name}.{meth} {}-{}", logical[m].first, logical[end(m) - 1].last));
                    }
                }
            }
        }
    }
    items
}

fn local_time() -> String {
    let t = SystemTime::now().duration_since(UNIX_EPOCH).map_or(0, |d| d.as_secs() as i64);
    let mut tm = Tm { sec: 0, min: 0, hour: 0, mday: 0, mon: 0, year: 0, wday: 0, yday: 0, isdst: 0, gmtoff: 0, zone: std::ptr::null() };
    unsafe {
        localtime_r(&t, &mut tm);
    }
    format!("{:04}-{:02}-{:02} {:02}:{:02}", tm.year + 1900, tm.mon + 1, tm.mday, tm.hour, tm.min)
}

/// Write the code map (file (lines): defs with line ranges) of root to out -> how many files (cm.py codemap()).
fn codemap(root: &Path, out: &Path, per: usize, exts: &[String]) -> io::Result<usize> {
    let mut rows = Vec::new();
    walk(root, "", &mut |p, rel| {
        if !exts.iter().any(|e| rel.ends_with(e.as_str())) {
            return;
        }
        let Ok(raw) = fs::read(p) else { return };
        let src = String::from_utf8_lossy(&raw);
        let defs: Vec<String> = if rel.ends_with(".py") {
            py_outline(&src)
        } else {
            splitlines(&src).iter().enumerate().filter_map(|(k, l)| def_name(l).map(|n| format!("{n} {}", k + 1))).collect()
        };
        let more = if defs.len() > per { format!(" (+{})", defs.len() - per) } else { String::new() };
        let shown = defs.iter().take(per).cloned().collect::<Vec<_>>().join(", ");
        rows.push(format!("- {rel} ({} lines): {shown}{more}", uni_lines(&src).len()));
    });
    let text = format!(
        "# Code map of {} ({})\n\nfile (lines): def first-last\n\n{}\n",
        abspath(root).display(),
        local_time(),
        rows.join("\n")
    );
    if let Some(dir) = out.parent().filter(|x| !x.as_os_str().is_empty()) {
        fs::create_dir_all(dir)?;
    }
    fs::write(out, text)?;
    Ok(rows.len())
}
