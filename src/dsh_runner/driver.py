"""Serial card driver: runs kanban cards through DSH headless and verifies the result.

One card at a time, on the configured branch of the repository:
  1. merge new planner commits (optional planner branch) and run the setup command;
  2. mirror card files onto the Hermes kanban board (idempotent);
  3. pick the first non-gate card whose dependencies are done;
  4. run DSH headless; the worker edits files but never commits;
  5. verify: only `allowed` files changed and the acceptance command passes when the driver re-runs it;
  6. commit on the worker's behalf and complete the card, or retry, then block it and move on.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time

from .board import Board
from .cards import Card, load_cards
from .config import Config

HEADER = """RUNNER NOTE (read first)
- You are the worker model running in the DSH headless harness. There are NO kanban tools: ignore any mention of kanban.
- Repository: {repo}. Work only inside it.
- Edit ONLY these files: {allowed}. Any other change makes the card fail.
- Do NOT run git add/commit/checkout/stash. The runner reviews and commits your work.
{rules}- WORKFLOW: inspect the named APIs, then write the implementation and run the acceptance command.
  Create or edit test files ONLY if they appear in the allowed list; a test file outside it fails the card.
  Existing contract tests are read-only. Make incremental edits; do not wait for a complete design.
- Never end your turn after only reading or planning: the same turn must write the file and run the tests.
- Acceptance command: `{accept}`. Iterate until it passes, then stop.
- If you are stuck on the same error twice, stop and explain the exact error in your final message.
- Final message: a short report of what you changed and the last acceptance output line.
{retry}
"""
NUDGE = ("Continue the unfinished implementation from the current files and session context. Write or fix code in "
         "{allowed} with the file tools in this turn, then run `{accept}` and fix failures until it passes.")


class Stopped(Exception):
    """STOP or PAUSE was requested while a card was running."""


class Unreachable(Exception):
    """DSH produced no output after every infrastructure retry."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(message: str) -> None:
    print(f"{now()} {message}", flush=True)


class Driver:
    def __init__(self, config: Config, board: Board | None = None):
        self.c = config
        self.board = board or Board(config.board)
        self.held: set[str] = set()
        self.last_session_end = 0.0

    # --- git and working tree ---------------------------------------------------------------------------------

    def git(self, *args: str, check: bool = True) -> str:
        result = subprocess.run(["git", "-C", str(self.c.repo), *args], capture_output=True, text=True)
        if check and result.returncode:
            raise RuntimeError(f"git {' '.join(args)}: {result.stderr.strip()}")
        return result.stdout.strip()

    def snapshot(self) -> dict[str, str]:
        """Content hash of every tracked or untracked, non-ignored file."""
        snap = {}
        for name in self.git("ls-files", "-co", "--exclude-standard").splitlines():
            path = self.c.repo / name
            if path.is_file() and not name.startswith(self.c.always_ignored):
                snap[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        return snap

    @staticmethod
    def changed(before: dict, after: dict) -> list[str]:
        return sorted(f for f in set(before) | set(after) if before.get(f) != after.get(f))

    def revert(self, paths) -> None:
        tracked = set(self.git("ls-files").splitlines())
        for name in paths:
            if name in tracked:
                self.git("checkout", "--", name)
            else:
                (self.c.repo / name).unlink(missing_ok=True)

    def dirty(self) -> list[str]:
        lines = self.git("status", "--porcelain").splitlines()
        return [line for line in lines if line[3:] and not line[3:].startswith(self.c.always_ignored)]

    # --- board ------------------------------------------------------------------------------------------------

    def done_ids(self, tasks: dict, cards: dict[str, Card]) -> set[str]:
        """Done on the board, or a commit whose subject is exactly "<id>: <title>" exists on the branch."""
        done = {cid for cid, task in tasks.items() if task.get("status") in ("done", "archived")}
        subjects = {f"{cid}: {card.title}": cid for cid, card in cards.items()}
        for line in self.git("log", "--format=%s", self.c.branch, check=False).splitlines():
            if line.strip() in subjects:
                done.add(subjects[line.strip()])
        return done

    def sync_board(self, cards: dict[str, Card]) -> dict:
        """Mirror the card files: create missing tasks, set dependency links exactly, promote ready ones."""
        tasks = self.board.tasks()
        for cid, card in cards.items():
            if cid not in tasks:
                self.board.create(card, self.c.assignee, self.c.repo)
        tasks = self.board.tasks()
        done = self.done_ids(tasks, cards)
        for cid, card in cards.items():
            if cid not in tasks:
                continue
            tid = tasks[cid]["id"]
            shown = self.board.show(tid)
            if shown and cid not in done:
                body = (shown.get("task") or {}).get("body", card.body)
                if tasks[cid].get("title") != card.task_title or body != card.body:
                    self.board.run("edit", tid, "--title", card.task_title, "--body", card.body)
            current = set(shown.get("parents", []))
            wanted = {tasks[d]["id"] for d in card.depends if d in tasks}
            for parent in sorted(wanted - current):
                self.board.run("link", parent, tid)
            for parent in sorted(current - wanted):
                self.board.run("unlink", parent, tid)
            if cid not in done and tasks[cid].get("status") == "todo" and all(d in done for d in card.depends):
                self.board.run("promote", tid)
        return self.board.tasks()

    # --- pacing and stop controls -----------------------------------------------------------------------------

    def stop_requested(self) -> bool:
        return self.c.stop_file.exists() or self.c.pause_file.exists()

    def cooldown(self) -> int:
        """<log_dir>/COOLDOWN_S (an integer, at least 60) overrides the configured cooldown while running."""
        try:
            return max(60, int((self.c.log_path / "COOLDOWN_S").read_text().strip()))
        except (OSError, ValueError):
            return self.c.cooldown_s

    def cool_down(self) -> bool:
        if self.last_session_end + self.cooldown() > time.time():
            log(f"cooling down {int(self.last_session_end + self.cooldown() - time.time())}s before the next session")
        while time.time() < self.last_session_end + self.cooldown():
            if self.stop_requested():
                return False
            time.sleep(min(3, self.c.poll_s))
        return True

    @contextmanager
    def model_lock(self):
        """One runner per model host: runners sharing a lock file take turns instead of overloading it."""
        self.c.model_lock.parent.mkdir(parents=True, exist_ok=True)
        with self.c.model_lock.open("a") as handle:
            announced = False
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if not announced:
                        log(f"another runner holds {self.c.model_lock}; waiting")
                        announced = True
                    if self.stop_requested():
                        raise Stopped
                    time.sleep(30)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    # --- DSH sessions -----------------------------------------------------------------------------------------

    def dsh_env(self) -> dict[str, str]:
        env = self.c.worker_env()
        dotenv = self.c.dsh_home / ".env"
        if dotenv.is_file():
            for line in dotenv.read_text().splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    key, value = line.split("=", 1)
                    env[key.strip()] = shlex.split(value)[0] if value.strip() else ""
        return env

    def dsh_setting(self, name: str) -> str:
        """The headless profile's default model or reasoning effort, for commit messages and comments."""
        patch = self.c.dsh_home / "profiles" / "headless" / "cordis.patch.yml"
        text = patch.read_text() if patch.is_file() else ""
        match = re.search(rf"id: agent-default-model\n(?:  .*\n)*?    {name}: (\S+)", text)
        return match.group(1) if match else "default"

    @staticmethod
    def read_events(path: Path) -> list[dict]:
        events = []
        if path.exists():
            for line in path.read_text(errors="replace").splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    events.append(event)
        return events

    def run_dsh(self, prompt: str, logdir: Path, session_id: str | None = None, tag: str = "",
                budget: float | None = None) -> dict:
        budget = self.c.run_timeout_min * 60 if budget is None else min(self.c.run_timeout_min * 60, budget)
        (logdir / f"prompt{tag}.txt").write_text(prompt)
        events_path = logdir / f"events{tag}.ndjson"
        if self.stop_requested() or not self.cool_down():
            events_path.write_text("")
            return dict(exit="stopped", minutes=0.0, seconds=0.0, tool_calls=0, output_tokens=0, final="",
                        session=session_id)
        start = time.monotonic()
        command = [self.c.dsh_command(), "headless", "--json", *(["--session-id", session_id] if session_id else []), "-"]
        with events_path.open("wb") as out, (logdir / f"stderr{tag}.txt").open("wb") as err:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=out, stderr=err, cwd=self.c.repo,
                                       env=self.dsh_env(), start_new_session=True)
            process.stdin.write(prompt.encode())
            process.stdin.close()
            try:
                while (code := process.poll()) is None:
                    if self.stop_requested():
                        kill_group(process)
                        code = "stopped"
                        break
                    if time.monotonic() - start >= budget:
                        kill_group(process)
                        code = "timeout"
                        break
                    time.sleep(self.c.poll_s)
            finally:
                self.last_session_end = time.time()
        events = self.read_events(events_path)
        seconds = time.monotonic() - start
        stats = dict(
            exit=code, minutes=round(seconds / 60, 1), seconds=seconds,
            tool_calls=sum(e.get("type") == "tool_call" for e in events),
            output_tokens=sum(e.get("usage", {}).get("outputTokens", 0) for e in events if e.get("phase") == "step_end"),
            final=next((e.get("text", "") for e in reversed(events) if e.get("type") == "final"), ""),
            session=next((e.get("sessionId") for e in events if e.get("type") == "session"), session_id),
        )
        (logdir / f"supervision{tag}.json").write_text(json.dumps(stats, indent=2))
        return stats

    @staticmethod
    def infra_failure(stats: dict) -> bool:
        """DSH exited with an error before the model produced anything (server down, network, credentials)."""
        return stats["exit"] not in (0, "timeout", "stopped") and not stats["tool_calls"] and not stats["output_tokens"]

    def run_dsh_retrying(self, prompt: str, logdir: Path, tid: str, session_id: str | None) -> dict:
        """Retry with backoff when DSH fails without output; such runs do not consume a card attempt."""
        for k in range(1, self.c.infra_retries + 1):
            stats = self.run_dsh(prompt, logdir, session_id=session_id)
            if not self.infra_failure(stats):
                return stats
            logdir.rename(logdir.with_name(f"{logdir.name}-infra{k}-{datetime.now():%H%M%S%f}"))
            logdir.mkdir()
            wait = self.c.infra_backoff_s * 2 ** (k - 1)
            log(f"infra failure (exit {stats['exit']}, no output); retry {k}/{self.c.infra_retries} in {wait}s")
            self.board.run("comment", tid, f"runner: DSH failed with no output (exit {stats['exit']}); infra retry "
                                           f"{k}/{self.c.infra_retries} in {wait}s, not counted as an attempt")
            deadline = time.time() + wait
            while time.time() < deadline:
                if self.stop_requested():
                    return {**stats, "exit": "stopped"}
                time.sleep(min(10, self.c.poll_s))
        return self.run_dsh(prompt, logdir, session_id=session_id)

    # --- acceptance and carried work --------------------------------------------------------------------------

    def acceptance(self, command: str, logdir: Path) -> tuple[bool, list[str]]:
        try:
            result = subprocess.run(command, shell=True, cwd=self.c.repo, capture_output=True, text=True,
                                    timeout=self.c.accept_timeout_min * 60, env=self.c.worker_env())
            output, ok = result.stdout + result.stderr, result.returncode == 0
        except subprocess.TimeoutExpired:
            output, ok = "acceptance TIMED OUT", False
        (logdir / "accept.log").write_text(output)
        return ok, output.strip().splitlines()[-25:]

    def invalid_card(self, card: Card) -> list[str]:
        """Files the acceptance command names must exist already or be among the card's allowed outputs."""
        gaps = []
        for token in shlex.split(card.accept):
            if "/" in token and re.fullmatch(r"[\w./-]+\.\w+", token) and not token.startswith(("/", "-")):
                if not (self.c.repo / token).is_file() and token not in card.allowed:
                    gaps.append(f"missing acceptance input outside allowed files: {token}")
        return gaps

    def evidence(self, logdir: Path, allowed: list[str]) -> str:
        """Concrete discoveries from earlier sessions (tool requests, file state); never private reasoning."""
        calls = []
        for path in sorted(logdir.glob("events*.ndjson")):
            for event in self.read_events(path):
                if event.get("type") == "tool_call":
                    args = event.get("input", {})
                    detail = args.get("file_path", args.get("command", args.get("pattern", "")))
                    calls.append(f"{event.get('tool')}: {str(detail)[:200]}")
        progress = {f: hashlib.sha256((self.c.repo / f).read_bytes()).hexdigest()
                    for f in allowed if (self.c.repo / f).is_file()}
        return ("Current allowed files: " + json.dumps(progress) +
                "\nRecent completed investigation/tool requests (reuse their results in this session):\n" +
                "\n".join(calls[-16:]))

    def carry_dir(self, cid: str) -> Path:
        return self.c.log_path / cid / "carry"

    def save_carry(self, card: Card, base: dict, stats: dict, logdir: Path, model: str) -> None:
        """Keep unfinished in-scope edits and run notes so a later run resumes instead of restarting."""
        carry = self.carry_dir(card.id)
        notes_path = carry / "notes.json"
        notes = json.loads(notes_path.read_text()) if notes_path.is_file() else {"runs": []}
        files = [f for f in self.changed(base, self.snapshot()) if f in card.allowed]
        if files:
            shutil.rmtree(carry / "files", ignore_errors=True)
            for name in files:
                if (self.c.repo / name).is_file():
                    dest = carry / "files" / name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes((self.c.repo / name).read_bytes())
            notes["files"] = {f: {"base": base.get(f), "deleted": not (self.c.repo / f).is_file()} for f in files}
        notes["runs"] = (notes["runs"] + [dict(
            at=now(), model=model, attempt=stats.get("attempt"), exit=stats.get("exit"), minutes=stats.get("minutes"),
            acceptance=stats.get("acceptance"), accept_tail=stats.get("accept_tail", []),
            final=(stats.get("final") or "")[-1500:],
            investigated=self.evidence(logdir, card.allowed).split("\n", 2)[-1][-2500:])])[-4:]
        carry.mkdir(parents=True, exist_ok=True)
        notes_path.write_text(json.dumps(notes, indent=2))

    def restore_carry(self, card: Card) -> str:
        """Restore carried files whose committed content is unchanged since they were saved; return a prompt note."""
        notes_path = self.carry_dir(card.id) / "notes.json"
        if not notes_path.is_file():
            return ""
        notes = json.loads(notes_path.read_text())
        current = self.snapshot()
        restored, stale = [], []
        for name, meta in notes.get("files", {}).items():
            if current.get(name) != meta["base"]:
                stale.append(name)
                continue
            if meta["deleted"]:
                (self.c.repo / name).unlink(missing_ok=True)
            else:
                (self.c.repo / name).parent.mkdir(parents=True, exist_ok=True)
                (self.c.repo / name).write_bytes((self.carry_dir(card.id) / "files" / name).read_bytes())
            restored.append(name)
        lines = ["\nPRIOR WORK ON THIS CARD (earlier runs, possibly by another model). Continue it; do not start over."]
        if restored:
            lines.append(f"Restored their unfinished edits into the working tree: {restored}")
        if stale:
            lines.append(f"Not restored because the committed file changed since: {stale}")
        for run in notes["runs"]:
            lines.append(f"- {run['at']} {run['model']} attempt {run['attempt']}: exit {run['exit']}, "
                         f"{run['minutes']} min, acceptance {run['acceptance']}")
            if run["accept_tail"]:
                lines.append("  acceptance tail: " + " | ".join(run["accept_tail"]))
            if run["final"]:
                lines.append("  its final message: " + run["final"].replace("\n", " ")[:800])
        if notes["runs"] and notes["runs"][-1]["investigated"]:
            lines.append("Last run already inspected (reuse, do not re-read everything):\n" +
                         notes["runs"][-1]["investigated"])
        return "\n".join(lines) + "\n"

    # --- one card ---------------------------------------------------------------------------------------------

    def prompt(self, card: Card, retry: str) -> str:
        rules = "".join(f"- {rule}\n" for rule in self.c.worker_rules)
        return HEADER.format(repo=self.c.repo, allowed=", ".join(card.allowed), accept=card.accept,
                             rules=rules, retry=retry) + "\n" + card.body

    def run_card(self, card: Card, task: dict) -> bool:
        gaps = self.invalid_card(card)
        if gaps:
            raise ValueError("Invalid card: " + "; ".join(gaps))
        tid, allowed, accept = task["id"], card.allowed, card.accept
        run_s = self.c.run_timeout_min * 60
        self.board.run("claim", tid, "--ttl", str(self.c.attempts * (run_s + self.c.accept_timeout_min * 60) +
                                                  self.c.infra_backoff_s * 2 ** self.c.infra_retries))
        model, effort = self.dsh_setting("model"), self.dsh_setting("reasoningEffort")
        self.board.run("comment", tid, f"runner: start {card.id} at {self.git('rev-parse', '--short', 'HEAD')} "
                                       f"(DSH {model}, effort {effort})")
        retry, continuation = "", None
        card_start = self.snapshot()
        prior = self.restore_carry(card)
        for attempt in range(1, self.c.attempts + 1):
            logdir = self.c.log_path / card.id / f"attempt-{attempt}"
            logdir.mkdir(parents=True, exist_ok=True)
            before = self.snapshot()
            prompt = self.prompt(card, retry) + ("\n" + prior if attempt == 1 and prior else "")
            attempt_start = time.monotonic()
            if attempt == 1 and prior and any(f in allowed for f in self.changed(card_start, self.snapshot())) \
                    and self.acceptance(accept, logdir)[0]:
                # Restored carried work already passes: commit it without spending a model session.
                (logdir / "prompt.txt").write_text(prompt)
                stats = dict(exit=0, minutes=0.0, seconds=0.0, tool_calls=0, output_tokens=0, final="",
                             session=None, carried=True)
            else:
                stats = self.run_dsh_retrying(prompt, logdir, tid, continuation)
            if self.infra_failure(stats):
                # The model never answered, so this says nothing about the card: release it and stop the runner,
                # since every other card would hit the same outage.
                self.board.run("comment", tid, f"runner: PAUSED, DSH produced no output after "
                                               f"{self.c.infra_retries} infra retries (exit {stats['exit']}); "
                                               "card not blocked, rerun once DSH is reachable")
                self.board.run("reclaim", tid, "--reason", "DSH unreachable")
                raise Unreachable(f"{card.id}: DSH unreachable; see {logdir}/stderr.txt")
            nudge = 0
            while stats["exit"] == 0 and not any(f in allowed for f in self.changed(before, self.snapshot())):
                # A turn that ended after only reading gets nudged to write, within the attempt's time budget.
                if any(f in allowed for f in self.changed(card_start, self.snapshot())) \
                        and self.acceptance(accept, logdir)[0]:
                    break
                left = min(run_s - (time.monotonic() - attempt_start), run_s - stats["seconds"])
                if left < self.c.min_nudge_s:
                    break
                nudge += 1
                more = self.run_dsh(NUDGE.format(allowed=", ".join(allowed), accept=accept) + "\n" +
                                    self.evidence(logdir, allowed), logdir, session_id=stats.get("session"),
                                    tag=f"-nudge{nudge}", budget=left)
                stats = {**more, "minutes": round(stats["minutes"] + more["minutes"], 1),
                         "seconds": stats["seconds"] + more["seconds"],
                         "tool_calls": stats["tool_calls"] + more["tool_calls"],
                         "output_tokens": stats["output_tokens"] + more["output_tokens"], "nudges": nudge}
            if stats["exit"] == "stopped":
                # Park partial edits in the carry and revert them, so the tree is clean and the card resumes later.
                stats.update(attempt=attempt, acceptance="not run")
                self.save_carry(card, card_start, stats, logdir, model)
                partial = self.changed(card_start, self.snapshot())
                self.revert(partial)
                (logdir / "result.json").write_text(json.dumps({**stats, "at": now(), "parked_edits": partial}, indent=2))
                self.board.run("comment", tid, f"runner: stopped; attempt not counted; partial edits carried and "
                                               f"reverted: {partial}")
                self.board.run("reclaim", tid, "--reason", "runner stopped")
                raise Stopped(card.id)
            after_worker = self.snapshot()
            touched = self.changed(before, after_worker)
            outside = [f for f in touched if f not in allowed]
            if outside:
                self.revert(outside)
                after_worker = self.snapshot()
            ok, tail = self.acceptance(accept, logdir)
            # The acceptance run may create allowed files (keep them) or rewrite unrelated ones (revert those).
            after_accept = self.snapshot()
            noise = [f for f in self.changed(after_worker, after_accept) if f not in allowed]
            if noise:
                self.revert(noise)
            touched = sorted(set(touched) | {f for f in self.changed(after_worker, after_accept) if f in allowed})
            stats.update(attempt=attempt, model=model, effort=effort, touched=touched, reverted_outside_scope=outside,
                         reverted_acceptance_noise=noise, acceptance="PASS" if ok else "FAIL", accept_tail=tail[-3:],
                         at=now())
            (logdir / "result.json").write_text(json.dumps(stats, indent=2))
            log(json.dumps({k: v for k, v in stats.items() if k != "final"}))
            # Commit every in-scope change since the card started, so a retry that fixes one file keeps the rest.
            in_scope = [f for f in self.changed(card_start, self.snapshot()) if f in allowed]
            if ok and not outside and in_scope:
                self.git("add", "--", *in_scope)
                self.git("commit", "-q", "-m",
                         f"{card.task_title}\n\nWritten by {model} (DSH headless, effort {effort}); attempt {attempt}, "
                         f"{stats['minutes']} min, {stats['tool_calls']} tool calls, {stats['output_tokens']} output "
                         f"tokens.\nRunner re-ran acceptance: {accept}\n{tail[-1] if tail else ''}")
                shutil.rmtree(self.carry_dir(card.id), ignore_errors=True)
                sha = self.git("rev-parse", "--short", "HEAD")
                summary = {k: stats[k] for k in ("minutes", "tool_calls", "output_tokens", "acceptance", "touched")}
                self.board.run("complete", tid, "--result", f"commit {sha}",
                               "--summary", f"{card.id} committed {sha}: " + json.dumps(summary))
                return True
            problems = []
            if outside:
                problems.append(f"You changed files outside the allowed list; they were reverted: {outside}.")
            if not in_scope:
                problems.append("No allowed file was changed.")
            if not ok:
                problems.append("Acceptance failed. Last output lines:\n" + "\n".join(tail))
            retry = ("\nPREVIOUS ATTEMPT FAILED. Your in-scope edits are still in place; fix them rather than starting "
                     "over.\n" + "\n".join(problems) + "\n" + self.evidence(logdir, allowed))
            continuation = stats.get("session")
            self.save_carry(card, card_start, stats, logdir, model)
            self.board.run("comment", tid, "runner: attempt {} failed: {}".format(attempt, " ".join(problems)[:1500]))
        # Blocked: the carry keeps the unfinished edits; reverting them keeps the tree clean for other cards.
        self.revert(self.changed(card_start, self.snapshot()))
        self.board.run("block", tid, "runner: attempts exhausted; see the card comments and carried work")
        return False

    # --- main loop --------------------------------------------------------------------------------------------

    def prepare(self) -> None:
        if self.c.planner_branch and self.git("rev-parse", "--verify", "-q", self.c.planner_branch, check=False):
            ahead = self.git("rev-list", "--count", f"{self.c.branch}..{self.c.planner_branch}")
            if ahead != "0":
                self.git("merge", "-q", "--no-edit", self.c.planner_branch)
                log(f"merged {ahead} planner commit(s)")
        if self.c.setup:
            # Install dependencies outside DSH, which may run without network access.
            subprocess.run(self.c.setup, cwd=self.c.repo, env=self.c.worker_env(), check=True)

    def next_card(self, cards: dict[str, Card], tasks: dict, only: str | None) -> tuple[Card | None, str]:
        done = self.done_ids(tasks, cards)
        pending = [card for cid, card in sorted(cards.items()) if cid not in done]
        if only:
            pending = [card for card in pending if card.id == only]
        ready = [card for card in pending if all(d in done for d in card.depends)
                 and tasks.get(card.id, {}).get("status") != "blocked"]
        if not ready:
            return None, f"no ready cards; pending: {[c.id for c in pending]}"
        runnable = [card for card in ready if not card.gate]
        if not runnable:
            return None, f"stopping at gate(s): {[c.id for c in ready]}"
        for card in runnable:
            gaps = self.invalid_card(card)
            if not gaps:
                return card, ""
            if card.id not in self.held:
                self.held.add(card.id)
                tid = tasks[card.id]["id"]
                self.board.run("comment", tid, f"runner: HELD, planner fix needed: {gaps}")
                self.board.run("block", tid, "runner: invalid acceptance inputs")
                log(f"held {card.id}: {gaps}")
        return None, f"no runnable cards; held: {sorted(self.held)}"

    def run(self, once: bool = False, only: str | None = None) -> int:
        if self.git("branch", "--show-current") != self.c.branch:
            sys.exit(f"{self.c.repo} must be on branch {self.c.branch}")
        self.c.log_path.mkdir(parents=True, exist_ok=True)
        while True:
            if self.c.stop_file.exists():
                log("STOP file present")
                return 0
            if self.c.pause_file.exists():
                log(f"paused by {self.c.pause_file}; waiting")
                time.sleep(30)
                continue
            dirty = self.dirty()
            if dirty:
                sys.exit(f"working tree dirty, refusing to run: {dirty}")
            self.prepare()
            cards = load_cards(self.c.cards_path, self.c.card_pattern)
            tasks = self.sync_board(cards)
            card, reason = self.next_card(cards, tasks, only)
            if card is None:
                log(reason)
                return 0
            if card.id not in tasks:
                sys.exit(f"card {card.id} missing on board {self.c.board}")
            try:
                with self.model_lock():
                    ok = self.run_card(card, tasks[card.id])
            except Stopped:
                log(f"{card.id}: stopped")
                return 0
            except Unreachable as exc:
                log(str(exc))
                return 3
            if not ok:
                log(f"{card.id} not completed; continuing with other ready cards")
            if once or only:
                return 0


def kill_group(process: subprocess.Popen) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=20)
            return
        except subprocess.TimeoutExpired:
            pass
