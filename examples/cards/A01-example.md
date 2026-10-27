---
id: A01
title: Add a slugify helper
depends: []
allowed: [src/myapp/text.py]
accept: python -m pytest -q tests/test_text.py
gate: false
---
Implement `slugify(title: str) -> str` in src/myapp/text.py.

- Lowercase, ASCII only: drop characters that are not letters, digits, spaces or hyphens.
- Collapse runs of spaces and hyphens into one hyphen; strip leading and trailing hyphens.
- Return "" for input that has no usable characters.

tests/test_text.py already exists and is read-only: it is the definition of done.
Do not add dependencies.
