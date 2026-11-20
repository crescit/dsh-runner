# dsh-runner

Work through a backlog of small coding cards with a local model, keeping only the commits you can trust.

`dsh-runner` takes cards from a [Hermes Agent](https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban)
kanban board. It hands each one to a model through the headless mode of the
[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) (`dsh`), then checks the result itself
before committing anything. The model never commits. The runner:

- reverts any file the card did not allow,
- re-runs the card's acceptance command itself instead of trusting the model's report,
- commits only work that passes, with a subject that marks the card done; if the acceptance command already passes
  on a clean checkout, it records a verified no-change completion without starting a model session,
- carries unfinished work into the next attempt or run instead of starting over.

Smaller and mid-sized local models finish far more work with a strict harness like this than they do as a
free-roaming agent. A card says exactly which files may change and how "done" is measured. The runner enforces
both.

**Status:** early (0.1). The loop comes from a private runner that has closed well over a hundred cards. This
packaged version is covered by end-to-end tests against fake `hermes` and `dsh`, and has been smoke-tested with a
real local model. Expect rough edges, and please open an issue when you hit one.

```
 card files (docs/cards/*.md)          Hermes kanban board                dsh headless
 ───────────────────────────  sync →  ─────────────────────  pick →  ┌──────────────────┐
  id, depends, allowed,                 one task per card              │ model edits files │
  accept, gate + instructions           links = dependencies           └────────┬─────────┘
                                                                                 ↓
               commit "<id>: <title>"  ←  pass  ←  re-run acceptance  ←  revert out-of-scope edits
               complete the task                    (runner, not model)
                                         fail  →  retry with the failure output → block + carry
```

## Requirements

- Python 3.11+ and git. The runner itself has no Python dependencies.
- [Hermes Agent](https://hermes-agent.nousresearch.com/) with its `hermes kanban` CLI. The board is the queue and
  the shared record of every card.
- The DeepSeek Harness CLI (`dsh`, npm package `@deepseek-ai/dsh`). It needs a **headless** profile whose default
  model points at your model server. Any OpenAI-compatible endpoint DSH can reach works, including your own
  local server.
- macOS or Linux. The runner uses process groups and `flock`.

## Setup

1. **Install the runner** from a clone:

   ```bash
   git clone https://github.com/crescit/dsh-runner.git
   pip install ./dsh-runner        # or: uv tool install ./dsh-runner
   ```

2. **Set up DSH with a headless profile.** Follow the DeepSeek Harness docs to install it under a home directory
   (`DSH_HOME`) and configure a provider for your model. Then check that a headless session answers:

   ```bash
   echo "Reply with READY." | DSH_HOME=~/.dsh dsh headless --json -
   ```

   The last line should be `{"type":"final","text":"READY"}`. If your model does not support reasoning effort,
   do not set a default `reasoningEffort` in the profile. DSH rejects it with `UNSUPPORTED_REASONING_EFFORT`.
   Put credentials for the provider in `$DSH_HOME/.env`. The runner loads that file into each session's
   environment, and it is never written to logs.

3. **Create a board** for the project:

   ```bash
   hermes kanban boards create my-project
   ```

4. **Add `dsh-runner.toml`** at the root of the repository the cards change. Start from
   [`examples/dsh-runner.toml`](examples/dsh-runner.toml). Then check the setup:

   ```bash
   cd path/to/your/repo
   dsh-runner check     # config, cards, dependencies, hermes on PATH, launches dsh, headless default model
   dsh-runner sync      # create one board task per card, with dependency links
   ```

5. **Run it**, in a terminal you leave open or under `nohup`, tmux, or a service manager:

   ```bash
   dsh-runner run              # until no ready cards remain
   dsh-runner run --once       # a single card
   dsh-runner run --card A03   # just this card
   dsh-runner run --workers 2  # two workers in managed worktrees (see Parallel runners)
   ```

   The runner refuses to start on the wrong branch or with uncommitted changes, because it needs to tell the
   model's edits apart from yours.

## Cards

Cards are Markdown files in `cards_dir` (default `docs/cards/`) with a small header:

```markdown
---
id: A02
title: Parse the settings file
depends: [A01]
allowed: [src/app/settings.py, tests/test_settings.py]
accept: python -m pytest -q tests/test_settings.py
gate: false
---
Implement `load_settings(path)` in src/app/settings.py. ...
```

| Field | Meaning |
|---|---|
| `id` | Letters then digits, with an optional letter suffix (`A02`, `O13b`). Board titles and commit subjects are `<id>: <title>`. |
| `depends` | Card ids that must be done first. A card counts as done when its task is done or a commit with its exact subject is on the branch; every sync then marks such a task done on the board too, parents first, because Hermes will not promote, claim or complete a task whose parents are open. |
| `allowed` | The only files the model may change. Everything else is reverted before acceptance runs. |
| `accept` | A shell command, run from the repository root by the runner. Exit status 0 means done, including when the
  card needs no edits and the clean checkout already passes. |
| `gate` | `true` holds the card for a human. Other ready cards keep running. |

A card whose `accept` command names a repository file (such as `tests/test_x.py`) that neither exists nor appears
in `allowed` is **held**: it is blocked with a comment saying what is missing, and the runner moves on.

Python bytecode (`__pycache__/`, `*.pyc`) never counts as an edit, and worker and acceptance runs set
`PYTHONDONTWRITEBYTECODE=1`, so running the tests does not trip the scope check.

Each commit's message records what produced it: the model and reasoning effort, the attempt number, minutes, tool
calls, output tokens, the acceptance command and its last output line.

What makes cards work well with weaker models:

- **Write the acceptance tests first, and keep them out of `allowed`.** A test file the model cannot edit is
  the strongest guarantee you can give: "done" means your tests pass, not the model's. Contract tests written by
  you or a stronger planner model work best.
- **One or two allowed files per card.** Name the functions, their signatures and the existing APIs to reuse.
- **Make the acceptance command fast and deterministic.** It runs after every attempt, and again whenever the
  runner needs to check carried work.
- **Split big cards** into a chain with `depends`, and gate the parts a human should look at first.
- **Review the commits.** Passing tests prove what the tests check, nothing more. Read the diffs, especially
  anything security-related.

See [`examples/cards/A01-example.md`](examples/cards/A01-example.md).

## Watching a run

- **Hermes dashboard:** `hermes dashboard` opens the web UI, where the board shows each card moving through
  ready, running, done or blocked. The runner comments on the task at every step: start, model, attempt
  failures with the acceptance output, the commit hash.
- **Terminal:** `hermes kanban --board my-project watch`, or `hermes kanban --board my-project show <task>` for
  one card's comments and events.
- **DSH web UI:** `dsh web` shows the harness and its sessions, which is useful for checking the profile and
  model before a long run.
- **Files:** each attempt writes `runs/board/<id>/attempt-N/`, containing the exact prompt, the session's NDJSON
  events, stderr, the acceptance log and `result.json` (minutes, tool calls, output tokens, files touched,
  reverted files, PASS/FAIL).

## Controls

| Action | How |
|---|---|
| Stop this runner now | `touch runs/board/STOP` (remove it to allow runs again) |
| Pause every runner on the machine | `touch ~/.dsh-runner/PAUSE` (configurable `pause_file`; remove it to resume) |
| Lengthen the gap between sessions | `echo 300 > runs/board/COOLDOWN_S` (seconds, minimum 60; read live) |
| Share one model server between repos | Point their `model_lock` at the same file, and the runners take turns |
| Run several workers in parallel | `dsh-runner run --workers 2` with one `[[slots]]` entry per model session; see below |

STOP takes effect immediately: a running session is killed, the card's partial edits are saved under
`runs/board/<id>/carry/` and reverted, the task is released, and the runner exits. The attempt is not counted, and
the next run restores those edits and tells the model what earlier attempts found.

PAUSE does the same to a runner that is mid-session, which then exits. A runner that is between cards, or waiting
for a model slot, waits until the file is removed.

If the model server is unreachable (DSH exits with an error and no output), the runner retries with backoff
without counting an attempt. If it is still down, it releases the card and exits with status 3. A `dsh` that
cannot start at all (exit 126 or 127, such as `env: node: No such file or directory`) is a setup problem, not an
outage: the runner stops at once with the error instead of retrying. Run `dsh-runner check` from the same
environment the runner uses (service manager, cron, `nohup`) to catch this before a long run.

| Exit status | Meaning |
|---|---|
| 0 | No ready cards remain, only gates remain, `--once`/`--card` finished, or STOP/PAUSE/Ctrl-C stopped the runner |
| 1 | Refused to run: wrong branch, uncommitted changes, a configuration error, or `dsh` cannot be launched at all (for example its `node` is not on `PATH`); the message says which, and the card is released |
| 3 | The model server was unreachable; the card was released, not blocked |

## Parallel runners

One command runs several workers from one repository:

```bash
dsh-runner run --workers 2      # or set [runner] workers = 2
```

Give the config one `[[slots]]` entry per model session your server can run at once (a llama.cpp server started
with `--parallel 4` has four), and use the same list in every project that shares that server:

```toml
[[slots]]
lock = "~/.dsh-runner/model.lock"
home = "~/.dsh-ml-infra"         # DSH home whose headless profile points at this server (default: [dsh] home)
[[slots]]
lock = "~/.dsh-runner/model2.lock"
home = "~/.dsh-ml-infra"
```

Without `[[slots]]` there is one slot made from `model_lock` and `[dsh] home`, as before. Workers beyond the number
of slots would only wait, so the runner starts at most that many.

**Worktrees are managed for you.** Worker *n* runs in its own git worktree on branch `dsh/w<n>`, under
`<slot lock dir>/worktrees/<board>/w<n>`, outside the repository. Each start resets them to `branch`; anything a
crashed worker left behind is first committed to a `dsh/salvage/w<n>-<time>` branch. `setup` runs in each worktree.
Workers share the main `log_dir`, so one STOP file stops them all and carried work resumes on any worker.

**Landing.** After a card passes, its worker merges `branch` into its own branch, re-runs the acceptance command if
that brought in new commits, and fast-forwards `branch`. If `branch` is checked out (your main checkout), git
fast-forwards that checkout itself and refuses rather than overwrite your local changes. Landings take turns.

**Conflicts never lose work.** If the merge conflicts, the merged acceptance run fails, or your checkout's local
changes are in the way, the commit is kept on `dsh/parked/<id>`. The card is blocked with a comment naming the
files and the branch, the worker goes back to `branch`, and other cards keep running. Cards that depend on it wait.
To resolve, merge it yourself (`git merge dsh/parked/A03` on `branch`); the next pass sees the commit, completes
the card on the board and runs its dependents.

**Also:**
- A runner takes a model slot before it claims a card, and holds a per-card lock while it works, so two workers
  (or runners in separate clones) never take the same card. Hermes' own atomic claim covers other machines.
- A task left `running` by a runner that died is reclaimed and resumed from its carried work.
- A card waits until its dependencies' runner commits are on its branch, and a runner waits, rather than exits,
  while other runners are working on cards that could unblock it.
- A card runs at most once per `dsh-runner run`. If it comes up again because the board refused to record the
  outcome (the refusal is logged and commented on the card), the runner stops instead of looping; fix the board
  and rerun.
- Ctrl-C (or SIGTERM) stops like STOP: partial edits are carried, the card is released. A second Ctrl-C kills.
- Every runner sharing a board or slots must be on this version: an older one takes no card locks.

## Configuration

All keys except `board` are optional. Unknown sections or keys are an error, so a typo cannot silently fall
back to a default.

```toml
[runner]
board = "my-project"            # Hermes kanban board slug
branch = "main"                 # the runner works and commits here
planner_branch = ""             # if set, merged into `branch` before each card (a planner can push new cards)
assignee = "dsh-runner"         # board assignee; keep it NOT a Hermes profile so the Hermes dispatcher ignores these cards
cards_dir = "docs/cards"
card_pattern = "[A-Z]*[0-9]*.md"
log_dir = "runs/board"          # add it to .gitignore
setup = ["uv", "sync", "--frozen", "-q"]     # runs before each card, outside DSH (which may have no network)
ignored = [".venv/", ".pytest_cache/"]       # never treated as worker edits
worker_rules = ["Run Python only through `uv run --frozen ...`."]   # extra lines for every prompt
env = { UV_CACHE_DIR = "./.uvcache" }        # values starting with ./ are resolved inside the repo

[dsh]
home = "~/.dsh"                 # DSH_HOME (default: $DSH_HOME or ~/.dsh)
bin = ""                        # default: $DSH_HOME/runtime/node_modules/.bin/dsh, then `dsh` on PATH

[limits]
run_timeout_min = 30            # hard cap per attempt, nudges included
accept_timeout_min = 15
attempts = 2
infra_retries = 4
infra_backoff_s = 60            # doubled on each retry
cooldown_s = 120                # idle gap between model sessions (spares a shared model server)
min_nudge_s = 180               # a session that only read gets nudged to write, if this much time remains
pause_file = "~/.dsh-runner/PAUSE"
model_lock = "~/.dsh-runner/model.lock"
```

## Safety model

The runner checks results; it does not sandbox the model. Know what that means before you run it.

- **Inside the repository** you get strong guarantees. Only `allowed` files ever reach a commit. Out-of-scope
  edits, and files the acceptance run rewrites, are reverted. Nothing is committed unless the runner's own
  acceptance run passes. Nothing is pushed.
- **Outside the repository**, the model can do whatever the DSH tools you enable allow, with your user's
  permissions: shell commands, network, files elsewhere. Use DSH's own permission and sandbox settings, run as an
  account without credentials you would not hand to the model, and keep secrets out of the repository and the
  prompt.
- **The acceptance command runs as you.** It is your code from the card, but the model can edit allowed files
  that the command imports or executes, so treat a card's allowed files as code the model controls.
- Logs contain prompts, tool calls and model output. Keep `log_dir` out of version control. Only the `$DSH_HOME/.env`
  values themselves stay out of logs.

## Development

```bash
python3 -m unittest discover -s tests -v
```

The tests drive the real runner against temporary git repositories, using fake `hermes` and `dsh`
executables (`tests/fakes/`) that replay scripted worker sessions. No model or network is needed.

## License

MIT. DeepSeek Harness and Hermes Agent are separate projects under their own licenses; this project is not
affiliated with either.
