"""Skills: folders with a SKILL.md whose frontmatter has a name and a one-line description. Only the index rides
each turn; `skill(name)` reads one when it is needed. Three places, later ones winning on a shared name: the built-in
skills that ship with the harness, BH_HOME/skills, and the seat's own skills folder."""

from __future__ import annotations

import re
from pathlib import Path

BUILTIN = Path(__file__).parent / "skills"
_FM = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)


def _meta(text: str) -> dict[str, str]:
    m = _FM.match(text)
    out: dict[str, str] = {}
    if m:
        for line in m.group(1).splitlines():
            k, _, v = line.partition(":")
            if v:
                out[k.strip()] = v.strip().strip('"')
    return out


class Skills:
    def __init__(self, *roots: Path):
        self.found: dict[str, Path] = {}
        self.desc: dict[str, str] = {}
        for root in (BUILTIN, *roots):
            if not root.exists():
                continue
            for f in sorted(root.glob("*/SKILL.md")):
                meta = _meta(f.read_text(encoding="utf-8"))
                name = meta.get("name") or f.parent.name
                self.found[name] = f
                self.desc[name] = meta.get("description", "")

    def index(self) -> str:
        return "\n".join(f"- {n}: {d}" for n, d in sorted(self.desc.items()))

    def read(self, name: str, part: str = "") -> str:
        f = self.found.get(name)
        if not f:
            return f"no skill {name!r}; skills: {', '.join(sorted(self.found))}"
        if part:
            p = (f.parent / part).resolve()
            if f.parent.resolve() not in p.parents or not p.is_file():
                return f"no file {part!r} in skill {name}: " + ", ".join(
                    str(x.relative_to(f.parent)) for x in f.parent.rglob("*") if x.is_file())
            return p.read_text(encoding="utf-8")
        return _FM.sub("", f.read_text(encoding="utf-8"), count=1)

    def search(self, words: list[str]) -> list[tuple[int, str, str]]:
        hits = []
        for n, d in self.desc.items():
            hay = (n + " " + d).lower()
            score = sum(2 if w in n.lower() else 1 for w in words if w in hay)
            if score:
                hits.append((score, n, d))
        return hits
