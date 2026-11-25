# code-mode

A code-mode skill for **Codex CLI** and **agy** (Antigravity CLI, Gemini), modelled on Claude Code's experimental
Code Mode: instead of many small tool calls, the agent writes **one Python script per step** and runs it through a
small runner, `cm`. The script fans out reads, greps and shell commands at once, keeps results in variables for the
next step, prints only a capped summary, and can edit + run the check in the same step. The skill also carries the
context rules of the studio-styles harness (`harness/CONTEXT.md`: find then read a range, map once, facts to
notes, a check before the code, one task per session) and the `studio` CLI rules (status / next, JSON out).

Why it could help: every model call re-reads the whole conversation, so the bill is roughly
`context x number of calls`. Fewer round trips and smaller outputs should mean faster and cheaper.

## What is in it

| path | what |
| --- | --- |
| `skills/code-mode/SKILL.md` | the instructions the agent reads (runner, helpers, step loop, context + studio + machine rules) |
| `rs/` | the Rust front `cm` (one crate, one binary, no outside crates): arguments, state folder, lock, saved outputs, the 6000-char cut, `cm out` / `vars` / `reset`, and the shell jobs, codemap and note the script asks for |
| `skills/code-mode/scripts/cm.py` | the step worker the front starts once per step (runs the script, keeps the variables), and the whole runner when there is no fresh binary: Python 3.9+, stdlib only |
| `install.sh` | links the skill the studio-styles way (store + Codex + agy), builds `rs/` and puts `cm` on PATH |
| `bench/bench.py` | the benchmark: fixture, tasks, checks, one run at a time, results table |
| `bench/results.jsonl` | every measured run (time, calls, tokens, answer, check output) |

`cm` helpers inside a script: `sh`, `par` (parallel jobs), `read` (line ranges), `grep`, `files`, `edit`
(exact anchor, refuses 0 or several matches), `write`, `j` / `studio` (JSON commands), `codemap` (writes
`notes/code-map.md` once), `note` (one fact per line to `notes/facts.md`), `show`. Top-level plain values persist
between steps (pickled per working folder in `$TMPDIR/code-mode`, dropped after 6 h idle); printed output is capped
at 6000 chars and the full text stays readable with `cm out`.

### Rust front (2026-11-25)

`cm` on PATH is a small Rust binary (`rs/`, 480 KB, std only; release profile lto + codegen-units 1 + strip + panic
abort; Rust 1.99). It reads the arguments, finds the state folder (same key and place as the Python runner, so both
share state), takes the lock, drops state after 6 h, numbers the step, saves the full output, cuts what is printed and
prints the footer; `cm out`, `cm vars` and `cm reset` never start Python. For a step it starts one Python worker
(`cm.py`, imported so its bytecode is cached, heavy modules imported only when used) and serves the worker's helpers
over two inherited pipes (pipes, not a Unix socket: Codex's sandbox refuses AF_UNIX): `sh` / `par` / `j` / `grep`
run as `bash -c` in their own process group, killed as a group at the timeout, side by side for `par`, in the
worker's current folder and environment; `codemap` (same output as the `ast` version, checked on 105 files) and `note`
are Rust too. When the step ends, any job group still running is killed (a script that crashed or was stopped no
longer leaves `sleep`s behind), and output a script's own `os.system` children print is now caught and saved too.

The step script itself stays Python: that is what Codex and agy write, and the kept variables are pickles.

Python fallback: when `rs/bin/cm` is older than `rs/src` / `Cargo.toml`, the front hands the whole call to
`python3 cm.py` unchanged and starts one background rebuild (at most every 10 minutes, log in
`rs/bin/background-build.log`), so no call waits for cargo. Without cargo, `install.sh` links `cm` to `cm.py`.

Per call (median of 25, old runner, Rust front and the Python fallback interleaved; this Mac, load ~2.5; cwd =
studio-styles `harness/`):

| case | Python cm (before) | Rust front | change | Python fallback (new cm.py) |
| --- | --- | --- | --- | --- |
| cold step (after reset, `x = 1`) | 34.1 ms | 20.9 ms | -39 % | 26.2 ms |
| trivial step (`y = 2`) | 34.1 ms | 21.3 ms | -38 % | 25.8 ms |
| `par`: 2 reads + grep + git | 47.3 ms | 33.0 ms | -30 % | 40.4 ms |
| `par`: 4 shell commands | 54.1 ms | 38.6 ms | -29 % | 46.0 ms |
| `sh` (cat 3 files) + print cut at 6000 | 39.4 ms | 26.0 ms | -34 % | 32.6 ms |
| `cm out 1:40` | 34.4 ms | 3.1 ms | -91 % | 25.2 ms |
| `cm vars` | 33.6 ms | 2.9 ms | -91 % | 25.1 ms |

Peak memory: `cm out` 28.8 MB -> 2.1 MB; a step 29 MB -> 21 MB (the Python worker). Next to a model call (seconds)
this is small; it adds up on long jobs with many steps and `cm out` slices.

## Install (Mac)

```bash
gh repo clone cyrus5x89/code-mode ~/Documents/git/code-mode
bash ~/Documents/git/code-mode/install.sh
```

This makes `~/.agents/skills/code-mode` a link to `skills/code-mode` here (the shared store, like support-mail and
openmotion), links `~/.codex/skills/code-mode` and `~/.gemini/config/skills/code-mode` (where agy loads skills) to
the store, builds the Rust front (`python3 skills/code-mode/scripts/cm.py --build`: `cargo build --release` of `rs/`,
copied to `rs/bin/cm` so `cargo clean` leaves it, about 5 s; needs Rust 1.99, `brew install rust`) and links `~/.local/bin/cm` to it, or to
`cm.py` when there is no cargo. After a pull that touched `rs/`, the first `cm` call runs in Python and rebuilds in
the background; to rebuild by hand run `cm.py --build` again or re-run `install.sh`. Claude is not linked: it has its own Code Mode. Edits in this
repo are live. Then add the CC Switch row (import the local skill in the app, tick Codex + agy, not Claude; the
app must be open for that, and closed for `ccswitch_skills.py update`), and check:
`python3 ~/Documents/git/studio-styles/tools/ccswitch_skills.py check`.

Use it by asking: "Use the code-mode skill for this task" (Codex can also take `$code-mode`).

## Benchmark (2026-11-25)

Fixture: a throwaway copy of studio-styles `harness/` + `tools/` (commit db71b31), fresh git copy per run.
Codex CLI 0.172.0, model gpt-6.2-sol (medium, Plus plan); agy 1.5.16, Gemini 4 Hydrogen (medium). One run at a time,
12 runs. `base` = the task prompt alone, run before the skill was installed; `cm` = the same prompt plus "Use the
code-mode skill for this task". Every cm run did use the runner (3-4 cm steps each).

- **t1** read-only question: 6 facts spread over neon.py, ctx_usage.py, studio.py, rs.py, script.py (graded per fact)
- **t2** bug fix: green/blue luminance weights swapped in `harness/color.py`; `checks/t2_check.py` must pass
- **t3** refactor: rename `paths.repos()` to `repo_path()` across 6 files without touching the `repos` key or the
  `<repos>` user text; `checks/t3_check.py` must pass

"input tokens (sum)" is what all model calls of the run read in total (cached + uncached): the re-read bill.
agy "out" includes thinking tokens.

| agent | task | mode | runs | wall s | model calls | input tokens (sum) | max ctx | out tokens | correct |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| agy | t1 | base | 1 | 29.2 | 5 | 156k | 47k | 3160 | 6/6 |
| agy | t1 | cm | 1 | 30.9 | 6 | 188k | 45k | 3975 | 6/6 |
| agy | t2 | base | 1 | 24.8 | 5 | 141k | 39k | 1662 | PASS |
| agy | t2 | cm | 1 | 35.9 | 5 | 149k | 42k | 1771 | PASS |
| agy | t3 | base | 1 | 54.9 | 8 | 274k | 48k | 6102 | PASS |
| agy | t3 | cm | 1 | 26 | 5 | 146k | 41k | 2636 | PASS |
| codex | t1 | base | 1 | 20.5 | 4 | 149k | 39k | 450 | 6/6 |
| codex | t1 | cm | 1 | 40.5 | 6 | 225k | 40k | 693 | 6/6 |
| codex | t2 | base | 1 | 20.5 | 4 | 150k | 40k | 488 | PASS |
| codex | t2 | cm | 1 | 40 | 7 | 273k | 42k | 907 | PASS |
| codex | t3 | base | 1 | 25.7 | 5 | 195k | 41k | 650 | PASS |
| codex | t3 | cm | 1 | 30.7 | 6 | 233k | 42k | 837 | PASS |

Reproduce: `python3 bench/bench.py setup`, then `python3 bench/bench.py run <codex|agy> <t1|t2|t3> <base|cm>`
or `all --cond base|cm` (base refuses to run while the skill is installed), then `python3 bench/bench.py table`.

### Re-run with the Rust front (2026-11-25, `cm-rs`)

Same fixture (studio-styles db71b31, rebuilt with `git archive`), same prompt as `cm`, Codex CLI 0.172.0 gpt-6.2-sol
medium and agy 1.5.16 Hydrogen medium, one run each, all six ran first time:

| agent | task | cm (Python runner) wall s / calls / input | cm-rs (Rust front) wall s / calls / input | correct |
| --- | --- | --- | --- | --- |
| agy | t1 | 30.9 / 6 / 188k | 49.9 / 7 / 229k | 6/6 |
| agy | t2 | 35.9 / 5 / 149k | 31.8 / 5 / 147k | PASS |
| agy | t3 | 26 / 5 / 146k | 32.4 / 5 / 147k | PASS |
| codex | t1 | 40.5 / 6 / 225k | 25.8 / 4 / 147k | 6/6 |
| codex | t2 | 40 / 7 / 273k | 32.5 / 6 / 227k | PASS |
| codex | t3 | 30.7 / 6 / 233k | 57.4 / 6 / 233k | PASS |

The runner saves ~13-15 ms a step and a run makes 2-5 steps, so under 0.1 s of a 25-57 s run: the wall-time swings
here are the model (codex t3: same 6 calls, +27 s) and how many calls it chose (agy t1 one more, codex t1 two fewer).
Same answers, nothing broke under Codex's sandbox or agy. The Rust front makes cm lighter, it does not change the
verdict below.

## Verdict

- **Smarter: no difference.** All 12 runs were correct (t1 6/6, t2 and t3 PASS, checks untouched) with and without
  the skill. These tasks were too easy to separate the two.
- **Faster for Codex: no, slower.** +5 to +20 s, +1 to +3 model calls and +19 to +82 % input tokens on every task.
  Codex already runs several shell calls per turn and has `rg`; the skill only adds a call to read SKILL.md (and in
  t1 one more to look at the runner) on jobs of 4-5 calls.
- **Faster for agy: only on the wide edit.** t3 (rename across 6 files): 55 s -> 26 s, 8 -> 5 calls, 274k -> 146k input
  tokens (-47 %), output 6.1k -> 2.6k. On the short question (t1) it was even (+2 s) and on the one-line fix (t2)
  slower (+11 s).
- The floor is the start prompt: every call already carries 35-48k tokens (system prompt, tools, skills list) before
  any work. Code mode can only cut the number of calls, so it pays when a job would otherwise take many calls
  (multi-file edits, sweeping reads, long fixes) and costs a little on short jobs.
- Single run per cell: treat differences under ~10 s as noise. Long jobs (30+ calls, where the gain should be
  largest) were not measured, to keep the benchmark at 12 runs.

So: keep it for agy on wide multi-file work and long jobs, ask for it by name; do not use it by default for Codex
or for short tasks. The skill description says the same, so it does not trigger itself on small jobs.
After the benchmark SKILL.md got two small fixes from the logs (do not open the runner's source; where the state
lives) and the narrower trigger above, and the runner got the review fixes (timeouts kill the whole process group,
per-variable state with a lock, CRLF kept, `j()` parses stdout only); they were not re-measured.
