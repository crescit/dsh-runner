"""Managed worktrees: `dsh-runner run --workers N` runs N runners from one repository.

Each worker is an ordinary runner in its own git worktree, on its own branch `dsh/w<n>`, outside the repository
(next to the slot locks), so test runners and editors in the main checkout never see the copies. Workers share the
main log directory (so STOP, COOLDOWN_S and carried work are shared) and land each card commit on the configured
branch themselves (see Driver.land). A commit that cannot land is parked on `dsh/parked/<id>`, never dropped.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime
from pathlib import Path
import signal
import subprocess
import sys

from .config import Config
from .driver import log


def worktree_root(base: Config) -> Path:
    return base.claims_path.parent.parent / "worktrees" / base.board


def worker_config(base: Config, n: int) -> Config:
    return dataclasses.replace(base, repo=worktree_root(base) / f"w{n}", branch=f"dsh/w{n}", land_to=base.branch,
                               log_dir=str(base.log_path), workers=1)


def git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and result.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout.strip()


def ensure_worktree(base: Config, n: int) -> Path:
    """Create worker n's worktree on `base.branch`, or reset a reused one to it, salvaging anything left behind."""
    path, branch = worktree_root(base) / f"w{n}", f"dsh/w{n}"
    git(base.repo, "worktree", "prune")
    if not (path / ".git").exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        git(base.repo, "worktree", "add", "-q", "-B", branch, str(path), base.branch)
        return path
    git(path, "checkout", "-q", branch, check=False)
    dirty = git(path, "status", "--porcelain")
    ahead = git(path, "rev-list", "--count", f"{base.branch}..HEAD")
    if dirty or ahead != "0":
        # Left over from a crash: keep it on a salvage branch, then start clean. Nothing is deleted unsaved.
        salvage = f"dsh/salvage/w{n}-{datetime.now():%Y%m%d-%H%M%S}"
        git(path, "checkout", "-q", "-b", salvage)
        if dirty:
            git(path, "add", "-A")
            git(path, "commit", "-q", "--no-verify", "-m", f"dsh-runner: salvaged worker w{n} state")
        log(f"w{n}: saved leftover work on {salvage}")
        git(path, "checkout", "-q", "-B", branch, base.branch)
    else:
        git(path, "reset", "-q", "--hard", base.branch)
    git(path, "clean", "-qfd")  # not -x: ignored files such as .venv survive
    return path


def supervise(base: Config, n: int, once: bool) -> int:
    """Run n workers as subprocesses and wait for all of them. Ctrl-C stops them cleanly; a second one kills."""
    slots = len(base.model_slots())
    if n > slots:
        log(f"{n} workers but only {slots} model slot(s); running {slots} (add [[slots]] for more)")
        n = slots
    for k in range(1, n + 1):
        ensure_worktree(base, k)
    command = [sys.executable, "-m", "dsh_runner", "-C", str(base.repo), *(["--config", str(base.config_file)]
               if base.config_file else []), "run", *(["--once"] if once else [])]
    procs = [subprocess.Popen([*command, "--worker", str(k)]) for k in range(1, n + 1)]
    log(f"started {n} workers in {worktree_root(base)}")
    codes = []
    try:
        codes = [proc.wait() for proc in procs]
    except KeyboardInterrupt:
        # The terminal already sent SIGINT to every worker; make sure, then wait for them to park their cards.
        for proc in procs:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
        try:
            codes = [proc.wait() for proc in procs]
        except KeyboardInterrupt:
            for proc in procs:
                proc.kill()
            return 130
    if any(code not in (0, 3) for code in codes):
        return 1
    return 3 if 3 in codes else 0
