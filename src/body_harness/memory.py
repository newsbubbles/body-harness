"""The harness's own memory: markdown files the agent reads and writes, outside the world.

Two shelves. `seat`: this character's notes (how its people answer, what worked, what the operator wants). `craft`:
lessons every seat shares about playing well. Each shelf has a MEMORY.md index, one line per note, that rides every
turn; the notes themselves are read on demand. What the character knows of the world lives in the world
(remember, intend, recall): this memory is the player's, not the character's."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

FILE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,60}\.md$")
INDEX_MAX = 4000  # characters of an index that ride every turn


class Shelf:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def index_path(self) -> Path:
        return self.root / "MEMORY.md"

    def index(self) -> str:
        t = self.index_path.read_text(encoding="utf-8") if self.index_path.exists() else ""
        if len(t) > INDEX_MAX:
            t = t[:INDEX_MAX] + "\n(index cut: tidy it, one short line per note)"
        return t

    def _file(self, name: str) -> Path:
        name = name.strip().lower()
        if not name.endswith(".md"):
            name += ".md"
        if name != "memory.md" and not FILE.match(name):
            raise ValueError("a note is a flat name: lowercase letters, digits, - and _, ending .md")
        return self.root / ("MEMORY.md" if name == "memory.md" else name)

    def read(self, name: str) -> str:
        p = self._file(name)
        return p.read_text(encoding="utf-8") if p.exists() else f"(no note {p.name}; list shows what there is)"

    def write(self, name: str, text: str, hook: str = "", append: bool = False) -> str:
        p = self._file(name)
        new = not p.exists()
        if append and p.exists():
            text = p.read_text(encoding="utf-8").rstrip() + "\n" + text
        p.write_text(text.rstrip() + "\n", encoding="utf-8")
        if p.name != "MEMORY.md":
            idx = self.index_path.read_text(encoding="utf-8") if self.index_path.exists() else ""
            line = f"- [{p.stem}]({p.name}): {hook.strip() or text.strip().splitlines()[0][:100]}"
            if f"]({p.name})" in idx:
                if hook:
                    idx = "\n".join(line if f"]({p.name})" in ln else ln for ln in idx.splitlines()) + "\n"
            else:
                idx = idx.rstrip() + "\n" + line + "\n"
            self.index_path.write_text(idx, encoding="utf-8")
        return f"{'wrote' if new else ('added to' if append else 'rewrote')} {p.name}"

    def list(self) -> list[str]:
        return sorted(p.name for p in self.root.glob("*.md"))

    def search(self, query: str, limit: int = 12) -> list[str]:
        q = query.lower()
        hits = []
        for p in sorted(self.root.glob("*.md")):
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if q in line.lower():
                    hits.append(f"{p.name}:{i}: {line.strip()[:200]}")
                    if len(hits) >= limit:
                        return hits
        return hits


def memo(shelves: dict[str, Shelf], op: str, name: str = "", text: str = "", hook: str = "", shelf: str = "seat",
         query: str = "") -> Any:
    s = shelves.get(shelf)
    if s is None:
        return {"ok": False, "reason": f"no shelf {shelf!r}: seat or craft"}
    try:
        if op == "list":
            return {"files": s.list(), "index": s.index()}
        if op == "read":
            return s.read(name or "MEMORY.md")
        if op in ("write", "append"):
            if not text:
                return {"ok": False, "reason": "nothing to write: give text"}
            return s.write(name, text, hook, append=op == "append")
        if op == "search":
            return s.search(query or text or name) or "no match"
    except ValueError as e:
        return {"ok": False, "reason": str(e)}
    return {"ok": False, "reason": f"op is list, read, write, append or search (got {op!r})"}
