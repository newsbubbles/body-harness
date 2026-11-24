"""A seat on disk: one folder per character the harness plays, under BH_HOME (default ~/.body-harness).

    BH_HOME/
      config.json            defaults for every seat: model, env_files, providers, prices
      craft/                 memory every seat shares: lessons about playing well, never one character's secrets
      skills/                skills every seat may read, beside the built-in ones
      seats/<name>/
        config.json          this seat's model and settings
        door.json            server and token (only the door reads it; the sandbox may not)
        state.json           loaded tools, recent turns, the watch, standing orders, the handbook list
        memory/              the harness's own notes for this seat (MEMORY.md is the index)
        workspace/           the sandbox: scratch code, and scene.json, the last look at the world
        apps/                tools this character built for itself
        skills/              skills for this seat alone
        inbox.jsonl          the operator's words to the seat (bh tell)
        outbox.jsonl         the seat's words to the operator, outside the fiction
        schedule.jsonl       what the seat asked to be woken for
        ledger.jsonl         one line per turn: why it woke, what it did, what the world said, what it cost
        usage.jsonl          one line per model call
        cache/               the last full perceive and handbook pages
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any


def home(override: str | None = None) -> Path:
    h = Path(override or os.environ.get("BH_HOME") or Path.home() / ".body-harness").expanduser()
    for sub in ("seats", "craft", "skills"):
        (h / sub).mkdir(parents=True, exist_ok=True)
    idx = h / "craft" / "MEMORY.md"
    if not idx.exists():
        idx.write_text("# Craft\n\nLessons every seat shares about playing well. One line per note: "
                       "- [title](file.md): hook\n", encoding="utf-8")
    return h


def read_json(p: Path, default: Any) -> Any:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(p: Path, data: Any) -> None:
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
    tmp.replace(p)


def append_jsonl(p: Path, row: dict[str, Any]) -> None:
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, default=str) + "\n")


def read_jsonl(p: Path) -> list[dict[str, Any]]:
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")


class Seat:
    def __init__(self, h: Path, name: str):
        if not NAME.match(name):
            raise ValueError(f"a seat name is lowercase letters, digits, - and _ (got {name!r})")
        self.home = h
        self.name = name
        self.dir = h / "seats" / name

    def create(self) -> "Seat":
        for sub in ("memory", "workspace", "apps", "skills", "cache"):
            (self.dir / sub).mkdir(parents=True, exist_ok=True)
        idx = self.dir / "memory" / "MEMORY.md"
        if not idx.exists():
            idx.write_text(f"# {self.name}\n\nThe harness's notes for this seat. One line per note: "
                           "- [title](file.md): hook\n", encoding="utf-8")
        return self

    @property
    def exists(self) -> bool:
        return (self.dir / "door.json").exists()

    def p(self, *parts: str) -> Path:
        return self.dir.joinpath(*parts)

    def config(self) -> dict[str, Any]:
        """The global defaults with this seat's settings over them."""
        cfg = read_json(self.home / "config.json", {})
        mine = read_json(self.p("config.json"), {})
        merged = {**cfg, **mine}
        merged["providers"] = {**cfg.get("providers", {}), **mine.get("providers", {})}
        merged["env_files"] = list(dict.fromkeys([*cfg.get("env_files", []), *mine.get("env_files", [])]))
        return merged

    def state(self) -> dict[str, Any]:
        return read_json(self.p("state.json"), {})

    def save_state(self, st: dict[str, Any]) -> None:
        write_json(self.p("state.json"), st)

    def outbox(self, text: str, kind: str = "note") -> None:
        append_jsonl(self.p("outbox.jsonl"), {"ts": time.time(), "kind": kind, "text": text})

    def inbox_new(self, after: float) -> list[dict[str, Any]]:
        return [r for r in read_jsonl(self.p("inbox.jsonl")) if r.get("ts", 0) > after]

    def tell(self, text: str) -> None:
        append_jsonl(self.p("inbox.jsonl"), {"ts": time.time(), "text": text})


def all_seats(h: Path) -> list[Seat]:
    return [Seat(h, d.name) for d in sorted((h / "seats").iterdir()) if d.is_dir() and (d / "door.json").exists()]
