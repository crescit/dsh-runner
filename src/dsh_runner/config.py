"""Runner configuration, read from dsh-runner.toml at the repository root."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import shutil
import tomllib

CONFIG_NAME = "dsh-runner.toml"


@dataclass
class Slot:
    """One model session at a time per slot: a lock file, and the DSH home whose profile points at that server."""
    lock: Path
    home: Path


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
    dsh_patch: Path | None = None
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
    slots: list[Slot] = field(default_factory=list)
    workers: int = 1
    # Set only for a managed worker (see workers.py): the branch its commits land on. Not a TOML key.
    land_to: str = ""
    config_file: Path | None = None

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

    def model_slots(self) -> list[Slot]:
        """The [[slots]] list, or the single slot that `model_lock` and `[dsh] home` describe."""
        return self.slots or [Slot(self.model_lock, self.dsh_home)]

    @property
    def claims_path(self) -> Path:
        """Per-card lock files shared by every runner on this host that works on the same board."""
        return self.model_slots()[0].lock.parent / "claims" / self.board

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
               "setup", "ignored", "worker_rules", "env", "workers"),
    "dsh": ("home", "bin", "patch"),
    "limits": ("run_timeout_min", "accept_timeout_min", "min_nudge_s", "attempts", "infra_retries",
               "infra_backoff_s", "cooldown_s", "poll_s", "pause_file", "model_lock"),
}
PATHS = {"pause_file", "model_lock"}
SLOT_KEYS = ("lock", "home")


def _path(value: str) -> Path:
    return Path(os.path.expandvars(value)).expanduser()


def load(repo: Path, path: Path | None = None) -> Config:
    path = path or repo / CONFIG_NAME
    if not path.is_file():
        raise SystemExit(f"no {CONFIG_NAME} at {path}; copy examples/{CONFIG_NAME} and edit it")
    data = tomllib.loads(path.read_text())
    slots = data.pop("slots", [])
    if not isinstance(slots, list) or not all(isinstance(slot, dict) for slot in slots):
        raise SystemExit(f"{path}: slots must be [[slots]] tables")
    unknown = [f"[{name}]" for name in data if name not in SECTIONS]
    unknown += [f"slots.{key}" for slot in slots for key in slot if key not in SLOT_KEYS]
    unknown += [f"{name}.{key}" for name, keys in SECTIONS.items() for key in data.get(name, {}) if key not in keys]
    if unknown:
        raise SystemExit(f"{path}: unknown setting(s): {', '.join(unknown)}")
    runner, dsh, limits = data.get("runner", {}), data.get("dsh", {}), data.get("limits", {})
    if not runner.get("board"):
        raise SystemExit(f"{path}: [runner] board is required")
    config = Config(repo=repo.resolve(), **runner)
    config.config_file = path.resolve()
    if not isinstance(config.workers, int) or config.workers < 1:
        raise SystemExit(f"{path}: [runner] workers must be a positive integer")
    for key, value in limits.items():
        setattr(config, key, _path(value) if key in PATHS else value)
    home = dsh.get("home") or os.environ.get("DSH_HOME")
    if home:
        config.dsh_home = _path(home)
    config.dsh_bin = dsh.get("bin", "")
    patch = dsh.get("patch")
    if patch:
        patch_path = _path(patch)
        if not patch_path.is_absolute():
            patch_path = config.repo / patch_path
        patch_path = patch_path.resolve()
        if not patch_path.is_file():
            raise SystemExit(f"{path}: [dsh] patch file does not exist: {patch_path}")
        config.dsh_patch = patch_path
    for slot in slots:
        if not slot.get("lock"):
            raise SystemExit(f"{path}: every [[slots]] entry needs a lock")
        config.slots.append(Slot(_path(slot["lock"]), _path(slot["home"]) if slot.get("home") else config.dsh_home))
    return config
