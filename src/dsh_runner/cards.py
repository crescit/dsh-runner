"""Card files: Markdown with a small front-matter header the runner reads.

    ---
    id: A02
    title: Parse the settings file
    depends: [A01]
    allowed: [src/app/settings.py, tests/test_settings.py]
    accept: python -m pytest -q tests/test_settings.py
    gate: false
    ---
    Free-form instructions for the worker model.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re

ID = re.compile(r"[A-Za-z]+[0-9]+[a-z]?")


@dataclass
class Card:
    id: str
    title: str
    accept: str
    allowed: list[str]
    depends: list[str] = field(default_factory=list)
    gate: bool = False
    body: str = ""
    path: Path | None = None

    @property
    def task_title(self) -> str:
        """Board titles and commit subjects are exactly "<id>: <title>"; done detection relies on it."""
        return f"{self.id}: {self.title}"


def parse_card(path: Path) -> Card | None:
    text = path.read_text()
    match = re.match(r"---\n(.*?)\n---\n", text, re.S)
    if not match:
        return None
    meta: dict = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            value = [item.strip() for item in value[1:-1].split(",") if item.strip()]
        meta[key.strip()] = value
    missing = [key for key in ("id", "title", "accept", "allowed") if not meta.get(key)]
    if missing:
        raise ValueError(f"{path}: card header is missing {', '.join(missing)}")
    if not ID.fullmatch(meta["id"]):
        raise ValueError(f"{path}: card id {meta['id']!r} must look like A01 or B12a")
    allowed = meta["allowed"] if isinstance(meta["allowed"], list) else [meta["allowed"]]
    depends = meta.get("depends") or []
    return Card(id=meta["id"], title=meta["title"], accept=meta["accept"], allowed=allowed,
                depends=depends if isinstance(depends, list) else [depends],
                gate=str(meta.get("gate", "false")).lower() == "true", body=text[match.end():], path=path)


def load_cards(directory: Path, pattern: str) -> dict[str, Card]:
    cards = [card for card in (parse_card(path) for path in sorted(directory.glob(pattern))) if card]
    seen: dict[str, Path] = {}
    for card in cards:
        if card.id in seen:
            raise ValueError(f"duplicate card id {card.id}: {seen[card.id]} and {card.path}")
        seen[card.id] = card.path
    return {card.id: card for card in cards}
