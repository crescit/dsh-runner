"""Runner configuration, read from dsh-runner.toml at the repository root."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import shutil
import tomllib

CONFIG_NAME = "dsh-runner.toml"


@dataclass
class Config:
    repo: Path
    board: str
    branch: str = "main"
    planner_branch: str = ""
    assignee: str = "dsh-runner"
    cards_dir: str = "docs/cards"
    card_pattern: str = "[A-Z]*[0-9]*.md"
    log_dir: str = "runs/board"
    setup: list[str] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)
    worker_rules: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    dsh_home: Path = Path.home() / ".dsh"
    dsh_bin: str = ""
    run_timeout_min: int = 30
    accept_timeout_min: int = 15
    min_nudge_s: int = 180
    attempts: int = 2
    infra_retries: int = 4
    infra_backoff_s: int = 60
    cooldown_s: int = 120
    poll_s: float = 3.0
    pause_file: Path = Path.home() / ".dsh-runner" / "PAUSE"
    model_lock: Path = Path.home() / ".dsh-runner" / "model.lock"

    @property
    def cards_path(self) -> Path:
        return self.repo / self.cards_dir

    @property
    def log_path(self) -> Path:
        return self.repo / self.log_dir

    @property
    def stop_file(self) -> Path:
        return self.log_path / "STOP"

    @property
    def always_ignored(self) -> tuple[str, ...]:
        # The runner's own logs never count as worker edits.
        return tuple(dict.fromkeys([self.log_dir.rstrip("/") + "/", *self.ignored]))

    def dsh_command(self) -> str:
        if self.dsh_bin:
            return str(Path(self.dsh_bin).expanduser())
        bundled = self.dsh_home / "runtime" / "node_modules" / ".bin" / "dsh"
        if bundled.is_file():
            return str(bundled)
        return shutil.which("dsh") or "dsh"

    def worker_env(self) -> dict[str, str]:
        """Environment for DSH and acceptance runs: DSH_HOME plus config env, with ./paths resolved in the repo."""
        env = os.environ.copy()
        env["DSH_HOME"] = str(self.dsh_home)
        env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
        for key, value in self.env.items():
            env[key] = str(self.repo / value) if value.startswith("./") else value
        return env


def _path(value: str) -> Path:
    return Path(os.path.expandvars(value)).expanduser()


def load(repo: Path, path: Path | None = None) -> Config:
    path = path or repo / CONFIG_NAME
    if not path.is_file():
        raise SystemExit(f"no {CONFIG_NAME} at {path}; copy examples/{CONFIG_NAME} and edit it")
    data = tomllib.loads(path.read_text())
    runner, dsh, limits = data.get("runner", {}), data.get("dsh", {}), data.get("limits", {})
    if not runner.get("board"):
        raise SystemExit(f"{path}: [runner] board is required")
    config = Config(repo=repo.resolve(), board=runner["board"])
    for key in ("branch", "planner_branch", "assignee", "cards_dir", "card_pattern", "log_dir",
                "setup", "ignored", "worker_rules", "env"):
        if key in runner:
            setattr(config, key, runner[key])
    if "home" in dsh or os.environ.get("DSH_HOME"):
        config.dsh_home = _path(dsh.get("home") or os.environ["DSH_HOME"])
    config.dsh_bin = dsh.get("bin", "")
    for key in ("run_timeout_min", "accept_timeout_min", "min_nudge_s", "attempts", "infra_retries",
                "infra_backoff_s", "cooldown_s", "poll_s"):
        if key in limits:
            setattr(config, key, limits[key])
    for key in ("pause_file", "model_lock"):
        if key in limits:
            setattr(config, key, _path(limits[key]))
    return config
