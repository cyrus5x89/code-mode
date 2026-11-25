#!/usr/bin/env python3
"""cm - a code-mode runner for Codex CLI and agy: one Python script per step instead of many small tool calls.

  cm <<'PY'            run a script (stdin); the value of the last line is printed, like a REPL
  ...
  PY
  cm -f step.py        run a script file
  cm vars              what is kept between steps (name, type, size)
  cm out [N] [a:b]     the full output of step N (default: last), optionally lines a..b
  cm reset             forget the kept variables and the saved outputs

Every model call re-reads the whole conversation, so the wins are fewer round trips (fan out reads, greps and
commands inside one script with par()) and smaller outputs (the printed text is capped; the full text is saved and
can be sliced with cm out). Plain values (str, list, dict, numbers ...) assigned at the top level are kept between
steps; modules, functions and open handles are not. The state is per working directory (CM_SESSION overrides),
lives in $CM_DIR or $TMPDIR/code-mode, and is dropped after CM_TTL hours (default 6) without a step.

Env: CM_CAP (printed chars per step, default 6000), CM_NOTES (fact file, default notes/facts.md), CM_TIMEOUT (sh
default, 300 s). Python 3.9+, stdlib only. Exit 1 when the script raised.

Rust front (rs/bin/cm, built by `python3 cm.py --build`, what ~/.local/bin/cm runs): it parses the arguments, keeps the state
folder, the lock, the step number and the saved outputs, cuts the printed text, and answers vars / out / reset itself.
Then this file is only the step worker (cm.worker(): run the script with the kept variables, save them back) and its
helpers hand shell jobs (sh, par, j, grep), codemap and note to it (CM_FDS). Run directly, or when the binary is
older than its sources (the front then hands the call here and rebuilds in the background), this file does it all.
"""
import ast, builtins, io, json, os, pickle, re, sys, time       # the rest is imported where it is used: a step starts fast
from pathlib import Path

CAP = int(os.environ.get("CM_CAP", "6000"))
TIMEOUT = int(os.environ.get("CM_TIMEOUT", "300"))
TTL = float(os.environ.get("CM_TTL", "6")) * 3600
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".next", "target", ".cache"}
_CH = None      # worker(): pipes to the Rust front that started it, which serves shell jobs, codemap and note


def state_dir():
    import hashlib, tempfile
    key = os.environ.get("CM_SESSION") or hashlib.sha1(os.getcwd().encode()).hexdigest()[:12]
    base = Path(os.path.abspath(os.environ.get("CM_DIR") or Path(tempfile.gettempdir()) / "code-mode"))
    d = base / key
    try:
        (d / "out").mkdir(parents=True, exist_ok=True)
    except OSError:                       # a sandbox that only lets us write the working directory
        d = Path(os.path.abspath(".cm")) / key
        (d / "out").mkdir(parents=True, exist_ok=True)
    return d


# ---------- helpers the script sees ----------

class R(str):
    """A shell result: the text (stdout + stderr) as a str, plus .code, .ok and .cmd."""
    code = 0
    cmd = ""

    @property
    def ok(self):
        return self.code == 0


R.__module__ = "cm"     # pickled as cm.R whether this file runs as the worker (module cm) or as the script (__main__)


def _r(text, code, cmd):
    r = R(text)
    r.code, r.cmd = code, cmd
    return r


def _txt(b):
    return b.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")    # what text=True gives


def _channel(fds):
    """Talk to the Rust front over the two pipes it left open (answers, requests): one reader thread hands each answer
    to the thread that asked, so a mixed par still runs its shell work side by side."""
    global _CH
    import threading
    rfd, wfd = map(int, fds.split(","))
    os.set_inheritable(rfd, False)        # not for the script's own children
    os.set_inheritable(wfd, False)
    rd, pending, lock = os.fdopen(rfd, "rb"), {}, threading.Lock()

    def reader():
        try:
            while head := rd.readline():
                rid, n = map(int, head.split())
                data = rd.read(n)
                with lock:
                    slot = pending.pop(rid, None)
                if slot:
                    slot[1] = data
                    slot[0].set()
        finally:
            with lock:
                pending["dead"] = True
                for k, slot in list(pending.items()):
                    if k != "dead":
                        slot[0].set()     # answer None: the caller does it in Python
    threading.Thread(target=reader, daemon=True).start()
    _CH = (pending, lock, threading.Lock(), wfd, [0], threading.Event)


def _ask(*parts):
    """One request to the Rust front, netstrings in, its answer out; None when it cannot be reached."""
    if _CH is None:
        return None
    pending, lock, wlock, wfd, ids, event = _CH
    msg = b"".join(b"%d:%s" % (len(p), p) for p in parts)
    slot = [event(), None]
    with lock:
        if "dead" in pending:
            return None
        ids[0] += 1
        rid = ids[0]
        pending[rid] = slot
    frame = memoryview(b"%d %d\n" % (rid, len(msg)) + msg)
    try:
        with wlock:
            while frame:
                frame = frame[os.write(wfd, frame):]
    except OSError:
        with lock:
            pending.pop(rid, None)
        return None
    slot[0].wait()
    return slot[1]


def _jobs(specs, merge, workers=4):
    """[(cmd, timeout, cwd, input)] run by the Rust front: bash -c in its own process group, the group killed at the
    timeout, `workers` at a time, in this process's folder and environment -> [(out, err, code) or OSError], or None
    when the front cannot be reached (then do it here)."""
    env = b"\0".join(k + b"=" + v for k, v in os.environb.items())
    parts = [b"jobs", b"%d" % max(1, min(workers, len(specs))), b"1" if merge else b"0", env]
    for cmd, t, c, i in specs:
        parts += [str(t or TIMEOUT).encode(), os.fsencode(os.path.abspath(c if c is not None else ".")),
                  b"0" if i is None else b"1", (i or "").encode("utf-8", "replace"), os.fsencode(cmd)]
    buf = _ask(*parts)
    if not buf:
        return None
    res, pos = [], 0
    try:
        for cmd, t, cwd, _ in specs:
            nl = buf.index(b"\n", pos)
            code, a, b, flag = map(int, buf[pos:nl].split())
            s = nl + 1
            out, err, pos = _txt(buf[s:s + a]), _txt(buf[s + a:s + a + b]), s + a + b
            if flag == 2:                 # bash did not start (a missing cwd ...): what Popen would have raised
                res.append(OSError(code, os.strerror(code), cwd) if code > 0 else OSError(out))
                continue
            if flag == 1:
                out += f"\n[timeout after {t or TIMEOUT} s, killed]"
            res.append((out, err, code))
    except ValueError:
        return None
    return res


def _run(cmd, timeout, cwd, input, merge):
    """bash -c in its own process group, so a timeout kills the whole pipeline, not only bash."""
    if _CH:
        r = _jobs([(cmd, timeout, cwd, input)], merge)
        if r is not None:
            if isinstance(r[0], OSError):
                raise r[0]
            return r[0]
    import signal, subprocess
    p = subprocess.Popen(["bash", "-c", cmd], stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT if merge else subprocess.PIPE,
                         text=True, errors="replace", cwd=cwd, start_new_session=True)
    try:
        out, err = p.communicate(input, timeout=timeout or TIMEOUT)
        return out or "", err or "", p.returncode
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        out, err = p.communicate()
        return (out or "") + f"\n[timeout after {timeout or TIMEOUT} s, killed]", err or "", 124


def sh(cmd, timeout=None, cwd=None, input=None):
    """Run a shell command (bash -c), stderr merged into stdout. Never raises: check .ok / .code."""
    out, _, code = _run(cmd, timeout, cwd, input, merge=True)
    return _r(out, code, cmd)


def par(*jobs, workers=4):
    """Run jobs at the same time and return their results in order. A job is a shell command (str) or a callable
    (lambda: read('a.py')). A job that raises returns the exception instead, so the others still count."""
    if len(jobs) == 1 and isinstance(jobs[0], (list, tuple)):
        jobs = tuple(jobs[0])
    if _CH and jobs and all(isinstance(x, str) for x in jobs):    # all shell: one request runs them side by side
        r = _jobs([(x, None, None, None) for x in jobs], True, workers)
        if r is not None:
            return [x if isinstance(x, OSError) else _r(x[0], x[2], c) for x, c in zip(r, jobs)]
    import threading

    def one(job):
        try:
            return sh(job) if isinstance(job, str) else job()
        except Exception as e:            # noqa: BLE001 - handed back to the script
            return e
    res, todo, lock = [None] * len(jobs), iter(range(len(jobs))), threading.Lock()

    def loop():
        while True:
            with lock:
                i = next(todo, None)
            if i is None:
                return
            res[i] = one(jobs[i])
    ts = [threading.Thread(target=loop) for _ in range(max(1, min(workers, len(jobs))))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return res


def read(path, a=None, b=None, n=False):
    """Text of a file, or of lines a..b (1-based, inclusive). n=True prefixes line numbers."""
    with open(Path(path).expanduser(), encoding="utf-8", errors="replace", newline="") as fh:
        lines = fh.read().splitlines()
    a = max(1, a or 1)
    b = min(len(lines), b or len(lines))
    sel = lines[a - 1:b]
    if n:
        w = len(str(b))
        sel = [f"{i:>{w}}| {s}" for i, s in enumerate(sel, a)]
    return "\n".join(sel)


def write(path, text):
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="") as fh:     # newline="": line endings stay as given
        fh.write(text)
    return len(text)


def edit(path, old, new, count=1):
    """Exact-string replace. count = how many matches must exist (0 = replace all, at least one).
    Raises when the file does not hold exactly that many, so a wrong anchor never writes."""
    with open(Path(path).expanduser(), encoding="utf-8", newline="") as fh:   # keep CRLF / LF as they are
        s = fh.read()
    k = s.count(old)
    if k == 0 or (count and k != count):
        raise ValueError(f"edit {path}: {k} matches for the anchor, wanted {count or 'at least 1'}")
    write(path, s.replace(old, new))
    return k


def _walk(root):
    for d, dirs, fs in os.walk(root):
        dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS and not x.startswith(".cm"))
        for f in sorted(fs):
            yield os.path.join(d, f)


def files(pattern="*", root=".", limit=2000):
    """Paths under root whose name (or relative path, when the pattern has a /) matches the glob."""
    import fnmatch
    out = []
    for p in _walk(root):
        rel = os.path.relpath(p, root)
        if fnmatch.fnmatch(rel if "/" in pattern else os.path.basename(p), pattern):
            out.append(rel if root == "." else p)
            if len(out) >= limit:
                break
    return out


def grep(pattern, *paths, i=False, fixed=False, glob=None, ctx=0, limit=200):
    """grep -rn: a list of 'path:line:text' (text cut at 240 chars), at most 'limit' lines; skips .git,
    node_modules, binaries. glob='*.py' limits the files."""
    cmd = ["grep", "-rnIs", "-E" if not fixed else "-F"]
    if i:
        cmd.append("-i")
    if ctx:
        cmd.append(f"-C{ctx}")
    for d in sorted(SKIP_DIRS):
        cmd.append(f"--exclude-dir={d}")
    if glob:
        cmd.append(f"--include={glob}")
    cmd += ["-e", pattern, "--", *map(os.fspath, paths or ["."])]
    out, err, code = _run(" ".join("'" + x.replace("'", "'\\''") + "'" for x in cmd), None, None, None, merge=False)
    if code > 1 and not out:                          # 2 with matches = one path was unreadable: keep the matches
        raise RuntimeError(err.strip()[:500] or f"grep exit {code}")
    lines = [ln[:240] for ln in out.splitlines()]
    if len(lines) > limit:
        lines = lines[:limit] + [f"... {len(lines) - limit} more matches (raise limit= or narrow the pattern)"]
    return lines


def j(cmd, timeout=None):
    """Run a command that prints JSON on stdout and return it parsed (stderr is ignored; the text when not JSON)."""
    out, err, code = _run(cmd, timeout, None, None, merge=False)
    try:
        return json.loads(out)
    except ValueError:
        return _r(out + err, code, cmd)


def studio(args, timeout=None):
    """The studio CLI (JSON out): studio('status') -> dict. Run from the project folder, or pass -C <dir>."""
    return j("studio " + args, timeout)


def note(*facts):
    """Append facts (one line each) to the notes file, so later steps and sessions read the note, not the source."""
    p = Path(os.environ.get("CM_NOTES", "notes/facts.md"))
    if _CH and _ask(b"note", os.fsencode(os.path.abspath(p)), *(str(f).encode("utf-8", "replace") for f in facts)) == b"0\n":
        return str(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        for f in facts:
            fh.write(f"- {' '.join(str(f).split())}\n")
    return str(p)


_DEF = re.compile(r"^\s*(?:export\s+)?(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:def|fn|function|class|struct|enum|impl|trait)\s+([A-Za-z_]\w*)")


def _outline(path):
    src = Path(path).read_text(encoding="utf-8", errors="replace")
    if path.endswith(".py"):
        try:
            tree = ast.parse(src)
        except SyntaxError:
            return []
        items = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                items.append(f"{node.name} {node.lineno}-{node.end_lineno}")
                if isinstance(node, ast.ClassDef):
                    items += [f"{node.name}.{m.name} {m.lineno}-{m.end_lineno}" for m in node.body
                              if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]
        return items
    out = []
    for k, line in enumerate(src.splitlines(), 1):
        m = _DEF.match(line)
        if m:
            out.append(f"{m.group(1)} {k}")
    return out


def codemap(root=".", out="notes/code-map.md", exts=(".py", ".js", ".mjs", ".ts", ".rs", ".go", ".sh", ".swift"),
            per_file=40):
    """Map once: write file (lines): top-level defs with line ranges to 'out' and return a one-line summary.
    Later steps read the map (grep(name, out)) instead of opening files to look around."""
    if _CH:
        r = _ask(b"codemap", os.fsencode(os.path.abspath(root)), os.fsencode(os.path.abspath(out)), b"%d" % per_file,
                 *map(os.fsencode, exts))
        if r and r.startswith(b"0 "):
            return f"{int(r.split()[1])} files mapped -> {out}"
    rows = []
    for p in _walk(root):
        if not p.endswith(exts):
            continue
        with open(p, encoding="utf-8", errors="replace") as fh:
            n = sum(1 for _ in fh)
        defs = _outline(p)
        more = f" (+{len(defs) - per_file})" if len(defs) > per_file else ""
        rows.append(f"- {os.path.relpath(p, root)} ({n} lines): " + ", ".join(defs[:per_file]) + more)
    head = f"# Code map of {os.path.abspath(root)} ({time.strftime('%Y-%m-%d %H:%M')})\n\nfile (lines): def first-last\n\n"
    write(out, head + "\n".join(rows) + "\n")
    return f"{len(rows)} files mapped -> {out}"


def show(x, n=None):
    """Print a value capped at n chars (head + tail kept)."""
    print(_cap(_fmt(x), n or CAP))


def _fmt(x):
    if isinstance(x, str):
        return x
    if isinstance(x, BaseException):
        return f"{type(x).__name__}: {x}"
    try:
        s = json.dumps(x, ensure_ascii=False, default=str)
        return s if len(s) < 160 else json.dumps(x, indent=1, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return repr(x)


def _cap(s, n):
    if len(s) <= n:
        return s
    head = s[: int(n * 0.6)]
    tail = s[len(s) - int(n * 0.35):]
    return f"{head}\n... [{len(s) - len(head) - len(tail)} chars cut; slice the full text: cm out [step] a:b] ...\n{tail}"


HELPERS = dict(sh=sh, par=par, read=read, write=write, edit=edit, files=files, grep=grep, j=j, studio=studio,
               note=note, codemap=codemap, show=show, R=R, Path=Path, os=os, re=re, json=json, sys=sys, time=time)


# ---------- the runner ----------

RESERVED = set(HELPERS) | set(dir(builtins))     # never kept: a kept list named "files" would break files() later


def load(d):
    """(vars, dropped). Each variable is pickled on its own, so one that cannot be loaded back (its class lived on a
    sys.path the last step added) drops only itself."""
    f = d / "state.pkl"
    if not f.exists():
        return {}, []
    try:
        st = pickle.loads(f.read_bytes())
    except Exception:                     # noqa: BLE001 - a broken state file starts fresh
        return {}, ["(state file unreadable)"]
    out, dropped = {}, []
    for k, b in st.get("vars", {}).items():
        try:
            out[k] = pickle.loads(b)
        except Exception:                 # noqa: BLE001
            dropped.append(k)
    return out, dropped


def _size(v):
    try:
        return len(v) if hasattr(v, "__len__") else ""
    except Exception:                     # noqa: BLE001 - a __len__ that raises
        return ""


def _atomic(p, data):
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, p)                    # a reader never sees half a file


def save(d, ns):
    """Pickle the plain top-level values to state.pkl and list them (name, type, size) in vars.txt for cm vars."""
    keep = {}
    for k, v in ns.items():
        if k.startswith("_") or k in RESERVED or callable(v) or type(v).__name__ == "module":
            continue
        try:
            keep[k] = pickle.dumps(v)
        except Exception:                 # noqa: BLE001 - not picklable: not kept
            continue
    _atomic(d / "state.pkl", pickle.dumps({"vars": keep}))
    rows = "".join(f"{k}\t{type(ns[k]).__name__}\t{_size(ns[k])}\n" for k in sorted(keep))
    _atomic(d / "vars.txt", rows.encode("utf-8", "replace"))
    return keep


# step number, saved outputs and expiry live in plain files, so the Rust front and this file share one state folder:
# step (last step number), out/N.txt (full output of step N), state.pkl + vars.txt (written by the step)

def last_step(d):
    """The step file; state from before it existed (step inside state.pkl): the highest saved output."""
    try:
        return int((d / "step").read_text())
    except ValueError:
        return 0
    except OSError:
        return max((int(f.stem) for f in d.glob("out/*.txt") if f.stem.isdigit()), default=0)


def expire(d):
    """Drop the kept state and the saved outputs after TTL without a step."""
    clock = [f.stat().st_mtime for f in (d / "step", d / "state.pkl") if f.exists()]
    if clock and time.time() - max(clock) > TTL:
        for x in [*d.glob("out/*.txt"), d / "state.pkl", d / "vars.txt", d / "step"]:
            x.unlink(missing_ok=True)


def reset(d):
    import shutil
    shutil.rmtree(d, ignore_errors=True)


def step(src, d, out):
    """Run one script with the variables kept in d, its prints and errors going to out; save the variables back
    -> (raised?, kept)."""
    vars_, dropped = load(d)
    ns = dict(vars_)
    ns.update(HELPERS)
    ns["__name__"] = "__cm__"
    err = False
    saved = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = out
    if dropped:
        print(f"[cm: could not load back {', '.join(dropped)} from the last step; recompute them]")
    try:
        tree = ast.parse(src, "<step>")
        last = tree.body.pop() if tree.body and isinstance(tree.body[-1], ast.Expr) else None
        exec(compile(tree, "<step>", "exec"), ns)
        if last is not None:
            v = eval(compile(ast.Expression(last.value), "<step>", "eval"), ns)
            if v is not None:
                print(_fmt(v))
    except SystemExit as e:               # sys.exit(0) / sys.exit() ends the step without an error
        err = e.code not in (None, 0)
        if err:
            print(f"SystemExit: {e.code}")
    except BaseException as e:            # noqa: BLE001 - report, keep what was assigned before the error
        import traceback
        err = True
        tb = traceback.format_exception(type(e), e, e.__traceback__.tb_next)   # drop the runner's own frame
        print("".join(tb).rstrip()[-1500:])
    finally:
        sys.stdout, sys.stderr = saved
        out.flush()
    return err, save(d, ns)


def worker():
    """The Rust front's step: the script on stdin, the state folder in CM_STATE_DIR. Prints go straight to stdout,
    line by line, so they stay in order with what the script's own children print there; the front saves it all,
    cuts it and prints the footer. Exit 1 when the script raised."""
    fds = os.environ.pop("CM_FDS", None)
    if fds:
        _channel(fds)
    d = Path(os.path.abspath(os.environ["CM_STATE_DIR"]))
    sys.argv = [__file__]
    setattr(sys.modules["__main__"], "R", R)       # R kept by the all-Python runner before 2026-11-25 (__main__.R)
    out = open(1, "w", encoding="utf-8", errors="replace", buffering=1, closefd=False)
    err, _ = step(sys.stdin.buffer.read().decode("utf-8", "replace"), d, out)
    return 1 if err else 0


def run(src, d):
    """The whole step without the Rust front: lock, expiry, number, saved output, cut, footer."""
    import fcntl
    with open(d / "lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)   # two cm calls in one folder take turns instead of losing state
        expire(d)
        n, t0 = last_step(d) + 1, time.time()
        buf = io.StringIO()
        err, kept = step(src, d, buf)
        text = buf.getvalue()
        (d / "out").mkdir(parents=True, exist_ok=True)
        (d / "out" / f"{n}.txt").write_text(text, encoding="utf-8", errors="replace")
        (d / "step").write_text(str(n))
        names = ", ".join(sorted(kept)[:12]) + (f" (+{len(kept) - 12})" if len(kept) > 12 else "")
        sys.stdout.write(_cap(text.rstrip("\n"), CAP) + "\n" if text.strip() else "")
        sys.stdout.write(f"[cm #{n} {'ERROR ' if err else ''}{time.time() - t0:.1f}s | {len(text)} chars"
                         f"{' | kept: ' + names if names else ''}]\n")
        return 1 if err else 0


def build():
    """cargo build --release of rs/, copy the binary to rs/bin/cm (what ~/.local/bin/cm links to; outside target/, so
    cargo clean leaves it) and stamp it
    with the build's start time, so a source saved during the build still counts as newer."""
    import shutil, subprocess
    crate = Path(__file__).resolve().parents[3] / "rs"
    cargo = shutil.which("cargo")
    if not cargo:
        print("cm --build: cargo not found (brew install rust); cm runs in Python until then")
        return 1
    t0 = time.time()
    tgt = crate / "target"
    env = {k: v for k, v in os.environ.items() if k not in ("CARGO_BUILD_TARGET", "CARGO_TARGET_DIR")}
    r = subprocess.run([cargo, "build", "--release", "--quiet", "--manifest-path", str(crate / "Cargo.toml"),
                        "--target-dir", str(tgt)], env=env)
    if r.returncode:
        return r.returncode
    out = crate / "bin"
    out.mkdir(exist_ok=True)
    tmp = out / f"cm.new-{os.getpid()}"
    shutil.copy2(tgt / "release" / "cm", tmp)
    os.replace(tmp, out / "cm")
    stamp = out / "cm.built"
    stamp.touch()
    os.utime(stamp, (t0, t0))
    print(f"cm: built {out / 'cm'}")
    return 0


def main(argv):
    if argv[:1] == ["--worker"]:
        return worker()
    if argv[:1] == ["--build"]:
        return build()
    d = state_dir()
    if argv[:1] == ["reset"]:
        reset(d)
        print("cm: state cleared")
        return 0
    if argv[:1] == ["vars"]:
        expire(d)
        try:
            rows = (d / "vars.txt").read_text(encoding="utf-8").splitlines()
        except OSError:
            rows = []
        rows = [r.split("\t") + ["", ""] for r in rows if r]
        for k, t, size, *_ in rows:
            print(f"{k}: {t} {size}")
        print(f"[{len(rows)} kept after step {last_step(d)}]")
        return 0
    if argv[:1] == ["out"]:
        expire(d)
        rest = argv[1:]
        n = int(rest.pop(0)) if rest and rest[0].isdigit() else last_step(d)
        f = d / "out" / f"{n}.txt"
        if not f.exists():
            print(f"cm: no output for step {n}")
            return 1
        lines = f.read_text(encoding="utf-8").splitlines()
        if rest and ":" in rest[0]:       # a range is printed whole: asking for it is the point
            a, b = (int(x) if x else None for x in rest[0].split(":", 1))
            print("\n".join(lines[(a or 1) - 1:b]))
        else:
            print(_cap("\n".join(lines), CAP))
        return 0
    if argv[:1] == ["-f"] and len(argv) > 1:
        src = Path(argv[1]).read_text(encoding="utf-8")
    elif argv[:1] in ([], ["run"], ["-"]):
        src = sys.stdin.read()
    else:
        print(__doc__.strip())
        return 2
    if not src.strip():
        print("cm: empty script (pass it on stdin: cm <<'PY' ... PY)")
        return 2
    return run(src, d)


if __name__ == "__main__":
    sys.modules.setdefault("cm", sys.modules["__main__"])     # so a kept cm.R loads back here too
    sys.exit(main(sys.argv[1:]))
