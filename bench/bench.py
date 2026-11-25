#!/usr/bin/env python3
"""bench - does the code-mode skill make Codex and agy faster / smarter? One run at a time (16 GB Mac rule).

  bench.py setup                         fixture: a throwaway copy of studio-styles (harness + tools) in WORK
  bench.py run <agent> <task> <cond>     agent = codex | agy, task = t1 | t2 | t3, cond = base | cm | cm-rs
  bench.py all --cond base|cm [--agent codex|agy] [--task t1 ...]
  bench.py table                         markdown table from results.jsonl (median when a cell ran twice)

Each run gets a fresh copy (git init, one commit) so runs never see each other. "cm" runs ask for the code-mode
skill; "base" runs are the same prompt without it (run them before the skill is installed); "cm-rs" is the same as
"cm", run with the Rust front of cm (2026-11-25), kept as its own row. Measured: wall time,
model calls, summed input tokens (what the calls re-read), max context, output tokens, and correctness (t1: facts
right out of 6; t2/t3: the check prints PASS and the check file is untouched). Python 3.9+, stdlib only.
"""
import argparse, json, os, re, shutil, statistics, subprocess, sys, tempfile, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
STUDIO = Path(os.environ.get("STUDIO_STYLES", Path.home() / "Documents/git/studio-styles"))
WORK = Path(os.environ.get("BENCH_WORK", Path.home() / ".cache/code-mode-bench"))
RESULTS = HERE / "results.jsonl"
CTX_USAGE = STUDIO / "harness/ctx_usage.py"
TIMEOUT = 1200
AGY_MODEL = os.environ.get("BENCH_AGY_MODEL", "gemini-4-hydrogen-medium")
CM_ASK = "Use the code-mode skill for this task (read its SKILL.md first, then work through the cm runner).\n\n"

T1 = """Answer from the code in this folder (read only, change nothing). Reply with ONE JSON object and nothing else:
{"neon_timeout_s": how many seconds one agy job may run by default in the Neon harness,
 "compact_env": the environment variable the Neon harness sets so agy compacts the conversation earlier,
 "doctor_func": the Python function that handles "studio doctor", as "file.py:function",
 "neon_price_in_out": [input, output] USD per 1M tokens the context-usage tool uses for Gemini 4 Neon,
 "rs_target": the folder the Rust studio-rs binary is built into,
 "slop_file": the file that holds the phrase list "studio script lint" checks}"""
T2 = """"python3 checks/t2_check.py" fails: the text-contrast ratios the harness reports are wrong for coloured text
(black on white is right). Find the bug in the harness code and fix it. Do not edit the check. When the check prints
PASS, reply with the file:line you changed and one sentence on the cause."""
T3 = """Rename the function repos() in harness/paths.py to repo_path() and update every caller in harness/ and tools/.
Keep the "repos" settings key, the STUDIO_REPOS env var and all user-facing text (like "<repos>/.env") as they are.
Then run "python3 checks/t3_check.py" until it prints PASS, and reply with the files you changed."""
TASKS = {"t1": T1, "t2": T2, "t3": T3}
T1_FACTS = {"neon_timeout_s": r"\b1800\b", "compact_env": r"^AGY_CLI_AUTO_COMPACT_OVERRIDE$",
            "doctor_func": r"studio\.py:cmd_doctor", "neon_price_in_out": r"^\[1\.5,\s*7\.5\]$",
            "rs_target": r"\.studio/rs-target", "slop_file": r"slop\.json"}


def sh(cmd, cwd=None, **kw):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, **kw)


# ---------------------------------------------------------------- fixture + tasks

def setup():
    fx = WORK / "fixture"
    shutil.rmtree(fx, ignore_errors=True)
    fx.mkdir(parents=True)
    ign = shutil.ignore_patterns("__pycache__", "gemini_web", "*.pyc", ".DS_Store")
    for d in ("harness", "tools"):
        shutil.copytree(STUDIO / d, fx / d, ignore=ign, symlinks=True)
    shutil.copy2(STUDIO / "CLAUDE.md", fx / "CLAUDE.md")
    sys.dont_write_bytecode = True                        # no __pycache__ inside the fixture
    sys.path.insert(0, str(fx / "harness"))
    import color, paths                                   # expected values come from the untouched code
    pairs = [((0, 255, 0), (0, 0, 0)), ((0, 0, 255), (255, 255, 255)), ((200, 30, 30), (250, 250, 250)),
             ((0, 0, 0), (255, 255, 255))]
    exp = {"pairs": [[list(a), list(b), round(color.contrast(a, b), 4)] for a, b in pairs],
           "placeholders": sum(p.read_text().count("<repos>") for p in (fx / "harness").glob("*.py"))}
    (WORK / "expected.json").write_text(json.dumps(exp, indent=1))
    print(json.dumps({"fixture": str(fx), **exp}))


CHECK_T2 = '''import json, sys
sys.path.insert(0, "harness")
import color
bad = [(a, b, want, round(color.contrast(tuple(a), tuple(b)), 4)) for a, b, want in PAIRS
       if abs(color.contrast(tuple(a), tuple(b)) - want) > 0.01]
for a, b, want, got in bad:
    print(f"contrast({a}, {b}) = {got}, want {want}")
print("FAIL" if bad else "PASS")
sys.exit(1 if bad else 0)
'''

CHECK_T3 = '''import glob, re, sys
sys.path.insert(0, "harness")
src = open("harness/paths.py").read()
errs = []
if not re.search(r"^def repo_path\\(", src, re.M): errs.append("no def repo_path( in harness/paths.py")
if re.search(r"^def repos\\(", src, re.M): errs.append("def repos( is still in harness/paths.py")
left = [f for f in glob.glob("harness/*.py") + glob.glob("tools/*.py") if "paths.repos(" in open(f).read()]
if left: errs.append(f"still calling paths.repos(: {left}")
for f in glob.glob("harness/*.py") + glob.glob("tools/*.py"):
    try: compile(open(f).read(), f, "exec")
    except SyntaxError as e: errs.append(f"syntax error: {f}: {e}")
n = sum(open(f).read().count("<repos>") for f in glob.glob("harness/*.py"))
if n != PLACEHOLDERS: errs.append(f"user-facing <repos> text changed: {n} left, want PLACEHOLDERS")
if not errs:
    import paths
    if paths.KEYS[0] != "repos": errs.append("the repos settings key changed")
    if paths.repo_path("jev", "bin") != paths.get("repos", "jev", "bin"): errs.append("paths.repo_path() gives another folder")
print("\\n".join(errs) or "PASS")
sys.exit(1 if errs else 0)
'''


def prepare(task, d):
    exp = json.loads((WORK / "expected.json").read_text())
    shutil.copytree(WORK / "fixture", d, symlinks=True)
    (d / "checks").mkdir()
    if task == "t2":
        f = d / "harness/color.py"
        s = f.read_text()
        good = "zip((0.2126, 0.7152, 0.0722), rgb)"
        assert s.count(good) == 1, "t2: anchor for the injected bug not found"
        f.write_text(s.replace(good, "zip((0.2126, 0.0722, 0.7152), rgb)"))     # green and blue weights swapped
        (d / "checks/t2_check.py").write_text(CHECK_T2.replace("PAIRS", json.dumps(exp["pairs"])))
    if task == "t3":
        (d / "checks/t3_check.py").write_text(CHECK_T3.replace("PLACEHOLDERS", str(exp["placeholders"])))
    (d / ".gitignore").write_text("__pycache__/\n*.pyc\n.cm/\n")
    sh(["git", "init", "-q"], d)
    sh(["git", "add", "-A"], d)
    sh(["git", "-c", "user.name=bench", "-c", "user.email=bench@local", "commit", "-qm", "fixture"], d)


def grade(task, d, answer):
    if task == "t1":
        m = re.search(r"\{.*\}", answer or "", re.S)
        try:
            got = json.loads(m.group(0)) if m else {}
        except ValueError:
            got = {}
        ok = {k: bool(re.search(p, json.dumps(got.get(k)) if isinstance(got.get(k), list) else str(got.get(k, "")).strip()))
              for k, p in T1_FACTS.items()}
        return {"score": f"{sum(ok.values())}/6", "pass": all(ok.values()), "wrong": [k for k, v in ok.items() if not v]}
    check = d / f"checks/{task}_check.py"
    changed = sh(["git", "diff", "--quiet", "HEAD", "--", f"checks/{task}_check.py"], d).returncode != 0
    r = sh([sys.executable, str(check)], d)
    diff = sh(["git", "diff", "--stat", "HEAD", "--", ".", ":!checks", ":!notes"], d).stdout.strip().splitlines()
    return {"pass": r.returncode == 0 and not changed, "check_edited": changed, "check_out": r.stdout.strip()[-300:],
            "diff": diff[-1] if diff else ""}


# ---------------------------------------------------------------- agents

def run_codex(prompt, d):
    last = d.parent / "last.txt"
    cmd = ["codex", "exec", "--json", "-s", "workspace-write", "--skip-git-repo-check", "-C", str(d), "-o", str(last), prompt]
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT, stdin=subprocess.DEVNULL)
        out, code = p.stdout, p.returncode
    except subprocess.TimeoutExpired as e:
        out, code = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or ""), -1
    secs = time.time() - t0
    (d.parent / "events.jsonl").write_text(out)
    tid = next((json.loads(ln).get("thread_id") for ln in out.splitlines() if '"thread.started"' in ln), None)
    m = {"secs": round(secs, 1), "exit": code, "session": tid}
    m.update(codex_usage(tid) or {})
    return m, last.read_text() if last.exists() else ""


def codex_usage(tid):
    if not tid:
        return None
    hits = list((Path.home() / ".codex/sessions").rglob(f"*{tid}*.jsonl"))
    if not hits:
        return None
    calls, ctx, tot, model, tools = 0, [], {}, None, 0
    for ln in hits[0].read_text().splitlines():
        e = json.loads(ln)
        p = e.get("payload") or {}
        if e.get("type") == "turn_context":
            model = p.get("model") or model
        if e.get("type") == "response_item" and p.get("type") in ("function_call", "custom_tool_call", "local_shell_call"):
            tools += 1
        if p.get("type") == "token_count" and p.get("info"):
            calls += 1
            ctx.append(p["info"]["last_token_usage"]["input_tokens"])
            tot = p["info"]["total_token_usage"]
    return {"model": model, "calls": calls, "tool_calls": tools, "input_sum": tot.get("input_tokens"),
            "cached": tot.get("cached_input_tokens"), "ctx_max": max(ctx) if ctx else None,
            "out": (tot.get("output_tokens") or 0)}


def run_agy(prompt, d):
    cmd = ["agy", "-p", prompt, "--model", AGY_MODEL, "--effort", AGY_MODEL.rsplit("-", 1)[1], "--output-format", "json",
           "--mode", "accept-edits", "--settings", '{"permissions":{"allow":["command(*)","read_file(*)"]}}']
    t0 = time.time()
    try:
        p = subprocess.run(cmd, cwd=d, capture_output=True, text=True, timeout=TIMEOUT, stdin=subprocess.DEVNULL)
        out, code = p.stdout, p.returncode
    except subprocess.TimeoutExpired:
        out, code = "", -1
    secs = time.time() - t0
    (d.parent / "agy.json").write_text(out)
    try:
        o = json.loads(out)
    except ValueError:
        o = {}
    conv = o.get("conversation_id")
    m = {"secs": round(secs, 1), "exit": code, "session": conv, "model": AGY_MODEL}
    if conv:
        u = sh([sys.executable, str(CTX_USAGE), "agy", conv, "--json"])
        try:
            r = json.loads(u.stdout)[0]
            t = r["totals"]
            m.update(calls=r["calls"], input_sum=t["read"] + t["uncached"], cached=t["read"], ctx_max=r["ctx_max"],
                     out=t["out"] + t["think"])
        except (ValueError, KeyError, IndexError):
            pass
    return m, o.get("response") or ""


def cm_dirs():
    """The runner's state folders (one per working dir), to count how many cm steps a run made."""
    base = Path(os.environ.get("CM_DIR") or Path(tempfile.gettempdir()) / "code-mode")
    return set(base.glob("*")) if base.is_dir() else set()


def run(agent, task, cond):
    if cond == "base" and (Path.home() / ".agents/skills/code-mode").exists():
        sys.exit("base runs need the skill uninstalled (rm the code-mode links), or the agent may pick it up itself")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    d = WORK / "runs" / f"{stamp}-{agent}-{task}-{cond}" / "repo"
    d.parent.mkdir(parents=True)
    prepare(task, d)
    prompt = (CM_ASK if cond != "base" else "") + TASKS[task]
    before = cm_dirs()
    m, answer = (run_codex if agent == "codex" else run_agy)(prompt, d)
    g = grade(task, d, answer)
    steps = sum(len(list((x / "out").glob("*.txt"))) for x in cm_dirs() - before) + len(list(d.glob(".cm/*/out/*.txt")))
    row = {"t": stamp, "agent": agent, "task": task, "cond": cond, **m, "cm_steps": steps, **g,
           "answer": (answer or "")[-600:],
           "dir": str(d.parent)}
    with RESULTS.open("a") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(json.dumps({k: row.get(k) for k in ("agent", "task", "cond", "secs", "calls", "input_sum", "pass", "score")}))
    return row


# ---------------------------------------------------------------- table

def table():
    rows = [json.loads(ln) for ln in RESULTS.read_text().splitlines() if ln.strip()]
    cells = {}
    for r in rows:
        cells.setdefault((r["agent"], r["task"], r["cond"]), []).append(r)
    med = lambda xs: statistics.median(xs) if xs else None
    print("| agent | task | mode | runs | wall s | model calls | input tokens (sum) | max ctx | out tokens | correct |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for (a, t, c) in sorted(cells):
        rs = cells[(a, t, c)]
        g = lambda k: med([r[k] for r in rs if r.get(k) is not None])
        ok = ", ".join(r.get("score") or ("PASS" if r.get("pass") else "FAIL") for r in rs)
        fmt = lambda v: "-" if v is None else (f"{v / 1000:.0f}k" if v >= 10000 else f"{v:g}")
        print(f"| {a} | {t} | {c} | {len(rs)} | {fmt(g('secs'))} | {fmt(g('calls'))} | {fmt(g('input_sum'))} | "
              f"{fmt(g('ctx_max'))} | {fmt(g('out'))} | {ok} |")


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("setup")
    r = sp.add_parser("run"); r.add_argument("agent", choices=["codex", "agy"]); r.add_argument("task", choices=list(TASKS))
    r.add_argument("cond", choices=["base", "cm", "cm-rs"])
    a = sp.add_parser("all"); a.add_argument("--cond", choices=["base", "cm", "cm-rs"], required=True)
    a.add_argument("--agent", choices=["codex", "agy"], action="append"); a.add_argument("--task", action="append")
    sp.add_parser("table")
    x = ap.parse_args()
    if x.cmd == "setup":
        setup()
    elif x.cmd == "run":
        run(x.agent, x.task, x.cond)
    elif x.cmd == "all":
        for t in x.task or list(TASKS):
            for ag in x.agent or ["codex", "agy"]:
                run(ag, t, x.cond)
    else:
        table()


if __name__ == "__main__":
    main()
