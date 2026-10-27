# dsh-runner

Run a backlog of small coding cards overnight with a local model, and wake up to commits you can trust.

`dsh-runner` takes cards from a [Hermes Agent](https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban)
kanban board. It hands each one to a model through the headless mode of the
[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) (`dsh`), then checks the result itself
before committing anything. The model never commits. The runner:

- reverts any file the card did not allow,
- re-runs the card's acceptance command itself instead of trusting the model's report,
- commits only work that passes, with a subject that marks the card done,
- carries unfinished work into the next attempt, or the next night, instead of starting over.

Smaller and mid-sized local models finish far more work with a strict harness like this than they do as a
free-roaming agent. A card says exactly which files may change and how "done" is measured. The runner enforces
both.

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
   dsh-runner check     # config, cards, dependencies, dsh and hermes on PATH, headless default model
   dsh-runner sync      # create one board task per card, with dependency links
   ```

5. **Run it**, in a terminal you leave open or under `nohup`, tmux, or a service manager:

   ```bash
   dsh-runner run              # until no ready cards remain
   dsh-runner run --once       # a single card
   dsh-runner run --card A03   # just this card
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
| `depends` | Card ids that must be done first. A card counts as done when its task is done or a commit with its exact subject is on the branch. |
| `allowed` | The only files the model may change. Everything else is reverted before acceptance runs. |
| `accept` | A shell command, run from the repository root by the runner. Exit status 0 means done. |
| `gate` | `true` holds the card for a human. Other ready cards keep running. |

What makes cards work well with weaker models:

- **Write the acceptance tests first, and keep them out of `allowed`.** A test file the model cannot edit is
  the strongest guarantee you can give: "done" means your tests pass, not the model's. Contract tests written by
  you or a stronger planner model work best.
- **One or two allowed files per card.** Name the functions, their signatures and the existing APIs to reuse.
- **Make the acceptance command fast and deterministic.** It runs after every attempt, and again whenever the
  runner needs to check carried work.
- **Split big cards** into a chain with `depends`, and gate the parts a human should look at first.
- **Review in the morning.** Passing tests prove what the tests check, nothing more. Read the diffs, especially
  anything security-related.

See [`examples/cards/A01-example.md`](examples/cards/A01-example.md).

## Watching a run

- **Hermes dashboard:** `hermes dashboard` opens the web UI, where the board shows each card moving through
  ready, running, done or blocked. The runner comments on the task at every step: start, model, attempt
  failures with the acceptance output, the commit hash.
- **Terminal:** `hermes kanban --board my-project watch`, or `hermes kanban --board my-project show <task>` for
  one card's comments and events.
- **DSH web UI:** `dsh web` shows the harness and its sessions, which is useful for checking the profile and
  model before an overnight run.
- **Files:** each attempt writes `runs/board/<id>/attempt-N/`, containing the exact prompt, the session's NDJSON
  events, stderr, the acceptance log and `result.json` (minutes, tool calls, output tokens, files touched,
  reverted files, PASS/FAIL).

## Controls

| Action | How |
|---|---|
| Stop after the current check | `touch runs/board/STOP` (remove it to allow runs again) |
| Pause every runner on the machine | `touch ~/.dsh-runner/PAUSE` (configurable `pause_file`) |
| Lengthen the gap between sessions | `echo 300 > runs/board/COOLDOWN_S` (seconds, minimum 60; read live) |
| Share one model server between repos | Point their `model_lock` at the same file, and the runners take turns |

When stopped or paused mid-card, the runner kills the session, saves the card's partial edits under
`runs/board/<id>/carry/`, reverts them, and releases the task. Your next run restores those edits and tells the
model what earlier attempts found.

If the model server is unreachable (DSH exits with an error and no output), the runner retries with backoff
without counting an attempt. If it is still down, it releases the card and exits with status 3, so a service
manager or wrapper can tell an outage from a finished queue.

## Configuration

All keys except `board` are optional:

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
cooldown_s = 120                # idle gap between model sessions (lets a busy GPU box cool down)
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
