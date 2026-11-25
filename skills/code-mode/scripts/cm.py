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
"""
import ast, contextlib, fnmatch, hashlib, io, json, os, pickle, re, shutil, subprocess, sys, tempfile, time, traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

CAP = int(os.environ.get("CM_CAP", "6000"))
TIMEOUT = int(os.environ.get("CM_TIMEOUT", "300"))
TTL = float(os.environ.get("CM_TTL", "6")) * 3600
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".next", "target", ".cache"}


def state_dir():
    key = os.environ.get("CM_SESSION") or hashlib.sha1(os.getcwd().encode()).hexdigest()[:12]
    base = Path(os.environ.get("CM_DIR") or Path(tempfile.gettempdir()) / "code-mode")
    d = base / key
    try:
        (d / "out").mkdir(parents=True, exist_ok=True)
    except OSError:                       # a sandbox that only lets us write the working directory
        d = Path(".cm") / key
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


def _r(text, code, cmd):
    r = R(text)
    r.code, r.cmd = code, cmd
    return r


def sh(cmd, timeout=None, cwd=None, input=None):
    """Run a shell command (bash -c), stderr merged into stdout. Never raises: check .ok / .code."""
    try:
        p = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, errors="replace", cwd=cwd,
                           input=input, timeout=timeout or TIMEOUT)
        return _r(p.stdout + p.stderr, p.returncode, cmd)
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"")
        out = out.decode(errors="replace") if isinstance(out, bytes) else out
        return _r(out + f"\n[timeout after {timeout or TIMEOUT} s]", 124, cmd)


def par(*jobs, workers=8):
    """Run jobs at the same time and return their results in order. A job is a shell command (str) or a callable
    (lambda: read('a.py')). A job that raises returns the exception instead, so the others still count."""
    if len(jobs) == 1 and isinstance(jobs[0], (list, tuple)):
        jobs = tuple(jobs[0])

    def one(job):
        try:
            return sh(job) if isinstance(job, str) else job()
        except Exception as e:            # noqa: BLE001 - handed back to the script
            return e
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(jobs) or 1))) as ex:
        return list(ex.map(one, jobs))


def read(path, a=None, b=None, n=False):
    """Text of a file, or of lines a..b (1-based, inclusive). n=True prefixes line numbers."""
    lines = Path(path).expanduser().read_text(encoding="utf-8", errors="replace").splitlines()
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
    p.write_text(text, encoding="utf-8")
    return len(text)


def edit(path, old, new, count=1):
    """Exact-string replace. count = how many matches must exist (0 = replace all, at least one).
    Raises when the file does not hold exactly that many, so a wrong anchor never writes."""
    p = Path(path).expanduser()
    s = p.read_text(encoding="utf-8")
    k = s.count(old)
    if k == 0 or (count and k != count):
        raise ValueError(f"edit {path}: {k} matches for the anchor, wanted {count or 'at least 1'}")
    p.write_text(s.replace(old, new), encoding="utf-8")
    return k


def _walk(root):
    for d, dirs, fs in os.walk(root):
        dirs[:] = sorted(x for x in dirs if x not in SKIP_DIRS and not x.startswith(".cm"))
        for f in sorted(fs):
            yield os.path.join(d, f)


def files(pattern="*", root=".", limit=2000):
    """Paths under root whose name (or relative path, when the pattern has a /) matches the glob."""
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
    cmd = ["grep", "-rnI", "-E" if not fixed else "-F"]
    if i:
        cmd.append("-i")
    if ctx:
        cmd.append(f"-C{ctx}")
    for d in sorted(SKIP_DIRS):
        cmd.append(f"--exclude-dir={d}")
    if glob:
        cmd.append(f"--include={glob}")
    cmd += ["-e", pattern, "--", *(paths or ["."])]
    p = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if p.returncode > 1:
        raise RuntimeError(p.stderr.strip()[:500])
    lines = [ln[:240] for ln in p.stdout.splitlines()]
    if len(lines) > limit:
        lines = lines[:limit] + [f"... {len(lines) - limit} more matches (raise limit= or narrow the pattern)"]
    return lines


def j(cmd, timeout=None):
    """Run a command that prints JSON and return it parsed (the text when it is not JSON)."""
    r = sh(cmd, timeout)
    try:
        return json.loads(r)
    except ValueError:
        return r


def studio(args, timeout=None):
    """The studio CLI (JSON out): studio('status') -> dict. Run from the project folder, or pass -C <dir>."""
    return j("studio " + args, timeout)


def note(*facts):
    """Append facts (one line each) to the notes file, so later steps and sessions read the note, not the source."""
    p = Path(os.environ.get("CM_NOTES", "notes/facts.md"))
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
    tail = s[-int(n * 0.35):]
    return f"{head}\n... [{len(s) - len(head) - len(tail)} chars cut; full text: cm out] ...\n{tail}"


HELPERS = dict(sh=sh, par=par, read=read, write=write, edit=edit, files=files, grep=grep, j=j, studio=studio,
               note=note, codemap=codemap, show=show, R=R, Path=Path, os=os, re=re, json=json, sys=sys, time=time)


# ---------- the runner ----------

def load(d):
    f = d / "state.pkl"
    if not f.exists():
        return {}, 0
    if time.time() - f.stat().st_mtime > TTL:
        reset(d)
        d.joinpath("out").mkdir(parents=True, exist_ok=True)
        return {}, 0
    try:
        st = pickle.loads(f.read_bytes())
        return st.get("vars", {}), st.get("step", 0)
    except Exception:                     # noqa: BLE001 - a broken state file starts fresh
        return {}, 0


def save(d, ns, step):
    keep = {}
    for k, v in ns.items():
        if k.startswith("_") or (k in HELPERS and v is HELPERS[k]):
            continue
        if callable(v) or type(v).__name__ == "module":
            continue
        try:
            pickle.dumps(v)
        except Exception:                 # noqa: BLE001 - not picklable: not kept
            continue
        keep[k] = v
    (d / "state.pkl").write_bytes(pickle.dumps({"vars": keep, "step": step}))
    return keep


def reset(d):
    shutil.rmtree(d, ignore_errors=True)


def run(src, d):
    vars_, step = load(d)
    step += 1
    ns = dict(HELPERS)
    ns.update(vars_)
    ns["__name__"] = "__cm__"
    buf, err, t0 = io.StringIO(), False, time.time()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            tree = ast.parse(src, "<step>")
            last = tree.body.pop() if tree.body and isinstance(tree.body[-1], ast.Expr) else None
            exec(compile(tree, "<step>", "exec"), ns)
            if last is not None:
                v = eval(compile(ast.Expression(last.value), "<step>", "eval"), ns)
                if v is not None:
                    print(_fmt(v))
        except BaseException as e:        # noqa: BLE001 - report, keep what was assigned before the error
            err = True
            tb = traceback.format_exception(type(e), e, e.__traceback__.tb_next)   # drop the runner's own frame
            print("".join(tb).rstrip()[-1500:])
    text = buf.getvalue()
    (d / "out" / f"{step}.txt").write_text(text, encoding="utf-8")
    kept = save(d, ns, step)
    names = ", ".join(sorted(kept)[:12]) + (f" (+{len(kept) - 12})" if len(kept) > 12 else "")
    sys.stdout.write(_cap(text.rstrip("\n"), CAP) + "\n" if text.strip() else "")
    sys.stdout.write(f"[cm #{step} {'ERROR ' if err else ''}{time.time() - t0:.1f}s | {len(text)} chars"
                     f"{' | kept: ' + names if names else ''}]\n")
    return 1 if err else 0


def main(argv):
    d = state_dir()
    if argv[:1] == ["reset"]:
        reset(d)
        print("cm: state cleared")
        return 0
    if argv[:1] == ["vars"]:
        vars_, step = load(d)
        for k, v in sorted(vars_.items()):
            size = len(v) if hasattr(v, "__len__") else ""
            print(f"{k}: {type(v).__name__} {size}")
        print(f"[{len(vars_)} kept after step {step}]")
        return 0
    if argv[:1] == ["out"]:
        _, step = load(d)
        rest = argv[1:]
        n = int(rest.pop(0)) if rest and rest[0].isdigit() else step
        f = d / "out" / f"{n}.txt"
        if not f.exists():
            print(f"cm: no output for step {n}")
            return 1
        lines = f.read_text(encoding="utf-8").splitlines()
        if rest and ":" in rest[0]:
            a, b = (int(x) if x else None for x in rest[0].split(":", 1))
            lines = lines[(a or 1) - 1:b]
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
    sys.exit(main(sys.argv[1:]))
