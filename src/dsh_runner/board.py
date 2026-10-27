"""Thin wrapper over the Hermes Agent kanban CLI (`hermes kanban --board <slug> ...`)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess

from .cards import ID, Card


class Board:
    def __init__(self, slug: str, hermes: str | None = None):
        self.slug = slug
        self.hermes = hermes or os.environ.get("DSH_RUNNER_HERMES", "hermes")

    def run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([self.hermes, "kanban", "--board", self.slug, *args],
                              capture_output=True, text=True, timeout=120)

    def tasks(self) -> dict[str, dict]:
        """Board tasks keyed by card id, taken from titles of the form "<id>: <title>"."""
        out = self.run("list", "--json")
        data = json.loads(out.stdout) if out.returncode == 0 and out.stdout.strip() else []
        tasks = data.get("tasks", data) if isinstance(data, dict) else data
        found = {}
        for task in tasks:
            match = re.match(rf"({ID.pattern}):", task.get("title", ""))
            if match:
                found[match.group(1)] = task
        return found

    def show(self, task_id: str) -> dict:
        out = self.run("show", task_id, "--json")
        return json.loads(out.stdout) if out.returncode == 0 and out.stdout.strip() else {}

    def create(self, card: Card, assignee: str, workdir: Path) -> None:
        # The assignee is deliberately not a Hermes profile, so the Hermes dispatcher never claims these cards.
        out = self.run("create", card.task_title, "--body", card.body, "--assignee", assignee,
                       "--workspace", f"dir:{workdir}", "--idempotency-key", f"{self.slug}-{card.id}", "--json")
        if out.returncode:
            print(f"warn: could not create board card {card.id}: {out.stderr.strip()}", flush=True)

    def edit(self, task_id: str, title: str, body: str) -> None:
        self.run("edit", task_id, "--title", title, "--body", body)

    def link(self, parent: str, child: str) -> None:
        self.run("link", parent, child)

    def unlink(self, parent: str, child: str) -> None:
        self.run("unlink", parent, child)

    def promote(self, task_id: str) -> None:
        self.run("promote", task_id)

    def claim(self, task_id: str, ttl_s: int) -> None:
        self.run("claim", task_id, "--ttl", str(ttl_s))

    def comment(self, task_id: str, text: str) -> None:
        self.run("comment", task_id, text)

    def complete(self, task_id: str, result: str, summary: str) -> None:
        self.run("complete", task_id, "--result", result, "--summary", summary)

    def block(self, task_id: str, reason: str) -> None:
        self.run("block", task_id, reason)

    def reclaim(self, task_id: str, reason: str) -> None:
        self.run("reclaim", task_id, "--reason", reason)

