---
name: code-mode
description: Code mode for Codex and agy - do each step of a job as ONE Python script run through the cm runner (fan out reads, greps and shell commands in parallel, keep results in variables between steps, print only a capped summary, edit and run the check in the same script, write facts to notes). Use when the user says code mode / cm, or for a wide job that reads or edits many files (multi-file rename or refactor, sweeping a repo for many facts, a long multi-step fix). Not worth it for a short task of a few tool calls.
---

# Code mode

Every model call re-reads the whole conversation. Ten small tool calls = ten re-reads of everything so far, and
every big output stays in the context for the rest of the job. Code mode cuts both: **one script per step** (many
actions, one round trip) and **small printed results** (big text stays in variables, not in the chat).

## The runner

```bash
cm <<'PY'
a, b, hits = par("git status -sb", lambda: read("src/app.py", 40, 90, n=True), lambda: grep("def load", "src"))
cfg = read("config.json")          # kept for the next step, not printed
print(a); print(b); hits[:20]       # the last line's value is printed too
PY
```

`cm` is `~/.local/bin/cm` (else `python3 ~/.agents/skills/code-mode/scripts/cm.py`); everything you need is on this
page, so don't open the runner's source. One Python process per step;
plain values (str, list, dict, numbers) assigned at top level are **kept for the next step** (per working folder,
in `$TMPDIR/code-mode`; no setup or env needed).
The printed text is capped at ~6000 chars (head + tail); the full text is saved: `cm out [step] [a:b]`.
`cm vars` lists what is kept, `cm reset` clears it. Exit 1 + traceback when the script raised; what was assigned
before the error is kept, so fix and go on from there.

Helpers (already imported, plus os re json sys time Path):

| call | does |
| --- | --- |
| `sh(cmd, timeout=300)` | bash -c, stderr merged; returns a str with `.ok` `.code`; never raises |
| `par(job, job, ...)` | runs jobs at once (str = shell command, callable = `lambda: ...`), results in order |
| `read(path, a, b, n=True)` | file text or lines a..b (1-based), n = line numbers |
| `grep(pat, *paths, glob="*.py", i=, fixed=, ctx=, limit=200)` | `path:line:text` list |
| `files("*.py", root)` | paths by glob (skips .git, node_modules, __pycache__ ...) |
| `edit(path, old, new, count=1)` | exact replace; raises unless the anchor matches `count` times (0 = all) |
| `write(path, text)` | whole file |
| `j(cmd)` / `studio("status")` | run a JSON-printing command, return it parsed |
| `codemap(root)` | writes `notes/code-map.md` (file -> defs with line ranges), once per repo |
| `note("fact")` | appends one line to `notes/facts.md` (CM_NOTES) |
| `show(x, n)` | print a value capped at n chars |

## The step loop

1. **Gather in one script.** Think what the decision needs, then fetch all of it at once with `par`: the greps,
   the line ranges, `git status`, the failing check. Not "one read, look, another read".
2. **Print the minimum.** Counts, the matching lines, the 30-60 lines that matter, `r.ok` - never a whole big
   file or a long log (`sh(...)[-2000:]` for the tail). Keep the rest in variables and slice them next step.
3. **Act in one script.** Edits (`edit` with exact anchors) + the check + `git diff --stat` in the same script; print
   the check's last 30 lines only when it fails.
4. Aim for **3-6 cm steps** on a small task. Shell one-liners outside cm are fine for a single trivial command;
   use the CLI's own patch tool when a big multi-hunk edit is easier that way.
5. **Finish** with the answer or "done + how it was checked", not a recap of what you read.

## Context rules (harness, studio-styles/harness/CONTEXT.md)

- **Find, then read a range.** grep the symbol / error text / flag first, read only that function.
  Whole files only when under ~300 lines or when you rewrite them. Never read a file twice: it is in a variable.
- **Map once.** First look at an unfamiliar repo: `codemap(".")` if `notes/code-map.md` is missing, then
  `grep(name, "notes/code-map.md")` instead of opening files to look around.
- **Facts to notes.** A fact you will need again (path:line, a value, a decision) goes in `note()`; later steps and
  later sessions read the note, not the source.
- **A check before the code.** A bug fix or feature names one command that fails now and passes when done; run it
  yourself, never "should work". Small diff, the file's own style, no reformat, no extra options.
- **One task per session.** An unrelated new task = a new session; carry facts in notes, not in the chat.
- **Don't learn a CLI from its source**: `<cmd> --help`, `studio status`.

## studio CLI (video projects, studio-styles)

Inside a project folder: `studio("status")` first and do its `next`; every studio command prints one JSON
object (`ok`, `errors`, `warnings`, `next`), exit 1 when not ok - read the fields you need, not the whole object
(`st = studio("status"); st["next"], st.get("errors")`). `studio <cmd> --help` for flags. Paid services need
`studio decide ... --by cyrus`; never add `--yes` to a paid generation yourself.

## Machine rules (Mac, 16 GB)

- `sh()` waits (default 300 s). Long jobs (render, install, long test, whisper) do **not** go inside cm: run them
  with the CLI's background tool, or once in the foreground with a long timeout; never poll with sleep loops.
- `par` is for I/O (reads, greps, git, quick commands). Never two heavy Python jobs (torch, whisper, render) at once.
- Stop every server / process you started before you finish.
