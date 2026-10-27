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


# Which dataclass fields each TOML section may set; anything else is a typo and an error.
SECTIONS = {
    "runner": ("board", "branch", "planner_branch", "assignee", "cards_dir", "card_pattern", "log_dir",
               "setup", "ignored", "worker_rules", "env"),
    "dsh": ("home", "bin"),
    "limits": ("run_timeout_min", "accept_timeout_min", "min_nudge_s", "attempts", "infra_retries",
               "infra_backoff_s", "cooldown_s", "poll_s", "pause_file", "model_lock"),
}
PATHS = {"pause_file", "model_lock"}


def _path(value: str) -> Path:
    return Path(os.path.expandvars(value)).expanduser()


def load(repo: Path, path: Path | None = None) -> Config:
    path = path or repo / CONFIG_NAME
    if not path.is_file():
        raise SystemExit(f"no {CONFIG_NAME} at {path}; copy examples/{CONFIG_NAME} and edit it")
    data = tomllib.loads(path.read_text())
    unknown = [f"[{name}]" for name in data if name not in SECTIONS]
    unknown += [f"{name}.{key}" for name, keys in SECTIONS.items() for key in data.get(name, {}) if key not in keys]
    if unknown:
        raise SystemExit(f"{path}: unknown setting(s): {', '.join(unknown)}")
    runner, dsh, limits = data.get("runner", {}), data.get("dsh", {}), data.get("limits", {})
    if not runner.get("board"):
        raise SystemExit(f"{path}: [runner] board is required")
    config = Config(repo=repo.resolve(), **runner)
    for key, value in limits.items():
        setattr(config, key, _path(value) if key in PATHS else value)
    home = dsh.get("home") or os.environ.get("DSH_HOME")
    if home:
        config.dsh_home = _path(home)
    config.dsh_bin = dsh.get("bin", "")
    return config
