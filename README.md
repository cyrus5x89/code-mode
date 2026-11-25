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
| `skills/code-mode/scripts/cm.py` | the runner: Python 3.9+, stdlib only, one process per step, no daemon |
| `install.sh` | links the skill the studio-styles way (store + Codex + agy) and puts `cm` on PATH |
| `bench/bench.py` | the benchmark: fixture, tasks, checks, one run at a time, results table |
| `bench/results.jsonl` | every measured run (time, calls, tokens, answer, check output) |

`cm` helpers inside a script: `sh`, `par` (parallel jobs), `read` (line ranges), `grep`, `files`, `edit`
(exact anchor, refuses 0 or several matches), `write`, `j` / `studio` (JSON commands), `codemap` (writes
`notes/code-map.md` once), `note` (one fact per line to `notes/facts.md`), `show`. Top-level plain values persist
between steps (pickled per working folder in `$TMPDIR/code-mode`, dropped after 6 h idle); printed output is capped
at 6000 chars and the full text stays readable with `cm out`. Python, not Rust: the agent's scripts are Python,
and the runner is a thin `exec` around them, so a Rust binary would only add a build step.

## Install (Mac)

```bash
gh repo clone cyrus5x89/code-mode ~/Documents/git/code-mode
bash ~/Documents/git/code-mode/install.sh
```

This makes `~/.agents/skills/code-mode` a link to `skills/code-mode` here (the shared store, like support-mail and
openmotion), links `~/.codex/skills/code-mode` and `~/.gemini/config/skills/code-mode` (where agy loads skills) to
the store, and links `~/.local/bin/cm` to the runner. Claude is not linked: it has its own Code Mode. Edits in this
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
(base runs need the skill uninstalled), `python3 bench/bench.py table`.

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
lives) and the narrower trigger above; they were not re-measured.
