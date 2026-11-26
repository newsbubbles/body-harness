"""The scene as menus: every block of the wake is a numbered list, and one tool opens an item (drill down) or shows
more of a list (its siblings, cut for space). Opt in with the seat setting `menu=true`; the scene card stays the
default.

Only the engine's vocabulary is used here: an entity's parent (here), its children (held), `connects_to` (ways),
its other edges by their relation name, `knows`, its own memory and goals, events and affordances. Labels are the
world's own: its type names, relation names and verb docs. A card the engine gives a `condition` is a creature
(something that can act), whatever the world calls its type. Nothing here names a kind of thing a world might have.

Numbers are this harness's tokens ("3.2", "4+"), never a world name, so they cannot be ambiguous and cannot clash
with a verb. An item opens through a read op (recall, inspect, history) or with text the harness already holds.

Two optional layers, each its own setting, so each can be measured alone:
- `menu_rank`: lists ordered by what this seat's goals and notes name and what was just said to it, then newest.
- `menu_preopen`: what a line said to the seat names is opened before the model asks, at the end of the wake.
"""

from __future__ import annotations

import json
import re
from typing import Any

from . import scene

STOP = {"what", "when", "where", "which", "about", "your", "have", "with", "that", "this", "there", "their", "from",
        "will", "would", "could", "should", "remind", "right", "tonight", "today", "again", "still", "just", "fen"}


def words(text: str) -> set[str]:
    found = (w.removesuffix("'s").strip("'-") for w in re.findall(r"[a-z][a-z'-]{3,}", str(text).lower()))
    return {w for w in found if len(w) > 3 and w not in STOP}


class Item:
    def __init__(self, line: str, opens: tuple[Any, ...], key: dict[str, float] | None = None, eid: str = ""):
        self.line, self.opens, self.key, self.eid = line, opens, key or {}, eid


class Widget:
    def __init__(self, title: str, items: list[Item], shown: int, sorts: tuple[str, ...] = (), order: str = "",
                 more: tuple[Any, ...] | None = None, empty: str = "", more_label: str = "older, from the log"):
        self.title, self.items, self.shown, self.sorts, self.order = title, items, shown, sorts, order
        self.more, self.empty, self.offset, self.more_label = more, empty, 0, more_label

    def sort(self, by: str) -> None:
        if by in self.sorts:
            self.items.sort(key=lambda i: -i.key.get(by, 0))
            self.order = by


class Menu:
    def __init__(self, b: dict[str, Any], names: scene.Names, me: str, day_seconds: float = scene.DAY,
                 events: list[dict[str, Any]] | None = None, rank: bool = False):
        names.learn(b)
        self.b, self.names, self.me, self.day = b, names, me, day_seconds
        you = b.get("you") or {}
        self.my_name = you.get("name") or ""
        self.events = events if events is not None else (b.get("events") or [])
        self.told = [e for e in self.events if scene.to_me(e, me, self.my_name)]
        st = you.get("state") or {}
        self.memory = [m for m in st.get("memory") or [] if isinstance(m, dict) and m.get("text")]
        self.goals = [g for g in b.get("goals") or [] if g.get("status", "active") == "active"]
        # what this seat cares about now: its goals and the lines said to it (rank uses it; preopen opens it)
        self.asked = words(" ".join(str((e.get("payload") or {}).get("text", "")) for e in self.told))
        self.wanted = words(" ".join(str(g.get("text", "")) for g in self.goals)) | self.asked
        self.rank = rank
        self.widgets: list[Widget] = []
        self._build()

    # ---- the blocks ----

    def _named(self, name: str) -> float:
        """How much this seat's goals, notes and the line to it name `name`: the model's own say in what matters."""
        n = str(name).lower()
        first = n.split()[0] if n else ""
        hits = sum(1 for t in [str(g.get("text", "")) for g in self.goals] + [str(m["text"]) for m in self.memory]
                   if n in t.lower() or (len(first) > 3 and re.search(rf"\b{re.escape(first)}\b", t.lower())))
        said = any(n in str((e.get("payload") or {}).get("text", "")).lower() or e.get("actor") in self._ids(name)
                   for e in self.told)
        return hits + (5 if said else 0)

    def _ids(self, name: str) -> set[str]:
        return {i for i, n in self.names.by_id.items() if n == name}

    def _tie(self, k: dict[str, Any]) -> str:
        day = lambda t: int(float(t or 0) // self.day) + 1
        bits = [f"met day {day(k.get('met_t'))}", f"{k.get('count', 0)} exchange{'' if k.get('count') == 1 else 's'}",
                f"standing {k.get('standing', 0)}"]
        ex = [e for e in k.get("last_exchanges") or [] if isinstance(e, dict) and e.get("text")]
        for mine in (True, False):
            e = next((e for e in reversed(ex) if (e.get("who") == self.me) == mine), None)
            if e:
                bits.append(f"{'you' if mine else 'they'} last said (day {day(e.get('t'))}): \"{str(e['text'])[:90]}\"")
        return "; ".join(bits)

    def _entity_line(self, x: dict[str, Any], knows: dict[str, Any]) -> str:
        line = scene._names([x], self.names)[0] + f" [{x.get('type', '?')}]"
        if x.get("id") in knows:
            line += ": " + self._tie(knows[x["id"]])
        elif x.get("condition") is not None:
            line += ": no meeting on record"
        return line

    def _build(self) -> None:
        b, me = self.b, self.me
        knows = {k.get("who"): k for k in b.get("knows") or [] if isinstance(k, dict)}
        here = b.get("here") or {}
        # here: who and what is in this place, those who can act first, then the rest
        crowd = b.get("present") if "present" in b else (b.get("people") or []) + (b.get("things") or [])
        present = [x for x in crowd or [] if isinstance(x, dict) and x.get("id") != me]
        items = []
        for x in present:
            k = knows.get(x.get("id")) or {}
            items.append(Item(self._entity_line(x, knows), ("op", "recall", {"about": x["id"]}), eid=x["id"],
                              key={"newest": float(k.get("last_t") or 0) + (1e12 if x.get("condition") is not None else 0),
                                   "named": self._named(x.get("name") or "")}))
        kinds: dict[str, int] = {}
        for x in present:
            kinds[x.get("type", "?")] = kinds.get(x.get("type", "?"), 0) + 1
        w = Widget(f"{here.get('name', 'Here')} ({', '.join(f'{n} {t}' for t, n in kinds.items()) or 'nobody else'})",
                   items, 8, ("newest", "named"), "newest", empty="nobody else")
        w.sort("named" if self.rank else "newest")
        if self.rank:  # those named first, then the newest: a stable two-key order
            w.items.sort(key=lambda i: (-i.key.get("named", 0), -i.key.get("newest", 0)))
        self.widgets.append(w)
        # held: this body's children
        held = list(b.get("holding") or [])
        self.widgets.append(Widget("Held", [Item(n, ("op", "inspect", {"entity": x["id"] if isinstance(x, dict)
                                                                       else scene.bare(x)}),
                                                 eid=x["id"] if isinstance(x, dict) else "")
                                            for x, n in zip(held, scene._names(held, self.names))], 8, empty="nothing"))
        # ways: connects_to
        ways = [x for x in b.get("exits") or [] if isinstance(x, dict)]
        self.widgets.append(Widget("Ways", [Item(x.get("name") or self.names.name(x.get("to")),
                                                 ("op", "inspect", {"entity": x.get("to")}), eid=str(x.get("to")))
                                            for x in ways], 10, empty="none"))
        # ties not here: knows, newest first (rank: those named first)
        here_ids = {x.get("id") for x in present}
        away = [k for k in knows.values() if k.get("who") not in here_ids and k.get("who") != me]
        ties = [Item(f"{self.names.name(k.get('who'))}: {self._tie(k)}", ("op", "recall", {"about": k.get("who")}),
                     eid=str(k.get("who")), key={"newest": float(k.get("last_t") or 0),
                                                 "most": float(k.get("count") or 0),
                                                 "standing": abs(float(k.get("standing") or 0)),
                                                 "named": self._named(self.names.name(k.get("who")))})
                for k in away]
        w = Widget("knows, not here", ties, 4, ("newest", "most", "standing", "named"), "newest", empty="nobody")
        w.sort("named" if self.rank else "newest")
        if self.rank:
            w.items.sort(key=lambda i: (-i.key.get("named", 0), -i.key.get("newest", 0)))
        self.widgets.append(w)
        # other edges, one block per relation name the world uses
        rels: dict[str, list[Item]] = {}
        for r in b.get("relations") or []:
            if not isinstance(r, dict) or r.get("relation") in ("knows", "connects_to") or not r.get("with"):
                continue
            arrow = "you ->" if r.get("direction") == "out" else "-> you"
            data = json.dumps(r.get("data") or {}, default=str)[:80] if r.get("data") else ""
            rels.setdefault(r["relation"], []).append(Item(f"{arrow} {r.get('name') or self.names.name(r['with'])}"
                                                           + (f" {data}" if data else ""),
                                                           ("op", "recall", {"about": r["with"],
                                                                             "relation": r["relation"]}),
                                                           eid=str(r["with"])))
        # memory: newest last on the card, as before; rank puts the notes that name what matters now first
        mem = [Item(str(m["text"])[:200], ("text", str(m["text"])), key={"newest": float(i), "named": float(len(
            words(m["text"]) & self.wanted))}) for i, m in enumerate(self.memory)]
        w = Widget("Your memory (this body's notes)", mem, 8, ("newest", "named"), "newest", empty="none")
        w.sort("named" if self.rank else "newest")
        if self.rank:
            w.items.sort(key=lambda i: (-i.key.get("named", 0), -i.key.get("newest", 0)))
        self.widgets.append(w)
        # goals
        self.widgets.append(Widget("Your goals", [Item(f"[{g.get('id')}] {g.get('text')}",
                                                       ("text", json.dumps(g, default=str))) for g in self.goals],
                                   8, empty="none"))
        # verbs: what this body can do now; more shows the refused ones with their reasons
        affs = scene.affordance_lines(self.b.get("affordances") or []) or [str(v) for v in self.b.get("verbs") or []]
        verbs = [Item(a, ("text", a)) for a in affs]
        self.widgets.append(Widget("You can", verbs, 30, more=("text", "\n".join(scene.affordance_lines(
            self.b.get("affordances") or [], True))), empty="nothing now", more_label="and what you cannot do now, why"))
        # events: said to you first, then the rest; more reads the world's log further back
        told = scene.event_lines(self.told, me, self.names, 10)
        rest = scene.event_lines([e for e in self.events if e not in self.told], me, self.names)
        first = min([int(e.get("seq") or 0) for e in self.events] or [0])
        hist = ("op", "history", {"until": first, "limit": 20, "newest": True}) if first else None
        talk = ("op", "history", {"until": first, "kinds": ["say", "talked"], "limit": 20, "newest": True})             if first else None
        self.widgets.append(Widget("Said to you", [Item(t, ("text", t)) for t in told], 10, more=talk, empty="",
                                   more_label="older talk, from the log"))
        self.widgets.append(Widget("Since you last looked", [Item(t, ("text", t)) for t in rest], 25, more=hist,
                                   empty=""))
        # the world's other relations last, one list each, so the fixed lists keep their numbers in every world
        for rel, its in rels.items():
            self.widgets.append(Widget(rel, its, 4))

    # ---- the view ----

    def render(self) -> str:
        b, you = self.b, self.b.get("you") or {}
        st = you.get("state") or {}
        vitals = []
        for k in ("hp", "water", "food", "wounded", "asleep", "bleeding", "tired"):
            if k in st and st[k] not in (None, False):
                vitals.append(f"{k} {st[k]}/{st['max_' + k]}" if f"max_{k}" in st else f"{k} {st[k]}")
        lines = [f"{scene.day_and_hour(float(b.get('t') or 0), b.get('ambient'), self.day)}; "
                 + "; ".join(f"{k} {v}" for k, v in (b.get("ambient") or {}).items() if k != "time_of_day")]
        lines.append(f"You are {you.get('name', '?')} [{you.get('type', '?')}]"
                     + (f" ({', '.join(vitals)})" if vitals else "") + f" at {(b.get('here') or {}).get('name', '?')}.")
        for n, w in enumerate(self.widgets, 1):
            if not w.items and not w.empty:
                continue
            head = f"{n} {w.title} ({len(w.items)})"
            if not w.items:
                lines.append(f"{head}: {w.empty}")
                continue
            lines.append(head + ":")
            for k, it in enumerate(w.items[:w.shown], 1):
                lines.append(f"  {n}.{k} {it.line}")
            left = len(w.items) - w.shown
            if left > 0 or w.more:
                tail = f"{left} more" if left > 0 else w.more_label
                sorts = f", by {' | '.join(w.sorts)}" if w.sorts else ""
                lines.append(f"  {n}+ {tail}{sorts}")
        o = b.get("owed")
        if o:
            lines.append(f"OWED: {o.get('by')} is waiting {o.get('min', 0):.0f} min for your answer about: {o.get('text')}")
        if b.get("owner_answered"):
            lines.append("The owner answered: " + json.dumps(b["owner_answered"], default=str)[:600])
        if b.get("rules_notice"):
            lines.append("RULES CHANGED: " + str(b["rules_notice"]))
        if b.get("reclaim"):
            lines.append("You may take back: " + json.dumps(b["reclaim"], default=str)[:300])
        lines.append('menu(open="1.2") opens an item; menu(more="4") shows more of list 4.')
        return "\n".join(lines)

    def resolve(self, at: str, by: str = "", find: str = "") -> tuple[Any, ...]:
        """A number -> ("text", str) or ("op", name, args) or ("error", why)."""
        at = str(at or "").strip()
        m = re.fullmatch(r"(\d+)(?:\.(\d+)|(\+))?", at)
        live = [str(n) for n, w in enumerate(self.widgets, 1) if w.items or w.empty]
        if not m or not (0 < int(m.group(1)) <= len(self.widgets)):
            return ("error", f"no item {at!r} on this menu", f"numbers here: {', '.join(live)} (and n.k, n+)")
        w = self.widgets[int(m.group(1)) - 1]
        if m.group(2):
            k = int(m.group(2))
            if not 0 < k <= len(w.items):
                return ("error", f"list {m.group(1)} has {len(w.items)} items", f"open {m.group(1)}.1 to "
                                                                                 f"{m.group(1)}.{len(w.items)}")
            return w.items[k - 1].opens
        # a whole list: more of it, optionally in another order or filtered by a word
        if by:
            if by not in w.sorts:
                return ("error", f"list {m.group(1)} sorts by {', '.join(w.sorts) or 'nothing'}", "")
            w.sort(by)
            w.offset = 0
        if not by and not find and w.offset == 0 and len(w.items) <= w.shown and w.more:
            return w.more  # the card showed all of it: more is what lies beyond, from the world
        its = [i for i in w.items if find.lower() in i.line.lower()] if find else w.items
        start = w.offset if not by and not find else 0
        if not find and not by and w.offset == 0:
            start = w.shown if len(its) > w.shown else 0  # a short list: all of it
        page = its[start:start + 12]
        if not page and w.more:
            return w.more
        w.offset = start + len(page)
        n = m.group(1)
        idx = {id(i): k for k, i in enumerate(w.items, 1)}
        rows = [f"  {n}.{idx[id(i)]} {i.line}" for i in page]
        left = len(its) - (start + len(page))
        foot = f"  {n}+ {left} more" if left > 0 else ("  (that is all)" if not w.more else f"  {n}+ {w.more_label}")
        return ("text", f"{w.title}, {len(its)} in all{f', by {w.order}' if w.order else ''}"
                        f"{f', matching {find!r}' if find else ''}:\n" + "\n".join(rows or ["  (none)"]) + "\n" + foot)

    def preopen(self) -> list[tuple[str, tuple[Any, ...]]]:
        """What the lines said to this seat name: the speaker and anyone named, and the notes that share the line's
        words, to be opened before the model asks."""
        out: list[tuple[str, tuple[Any, ...]]] = []
        if not self.told:
            return out
        seen = set()
        text = " ".join(str((e.get("payload") or {}).get("text", "")) for e in self.told).lower()
        for n, w in enumerate(self.widgets, 1):
            for k, it in enumerate(w.items, 1):
                if not it.eid or it.eid in seen or it.opens[0] != "op":
                    continue
                name = self.names.name(it.eid).lower()
                first = name.split()[0] if name else ""
                spoke = any(e.get("actor") == it.eid for e in self.told)
                if spoke or (len(first) > 2 and re.search(rf"\b{re.escape(first)}\b", text)):
                    seen.add(it.eid)
                    out.append((f"{n}.{k} {self.names.name(it.eid)}", it.opens))
        goals = words(" ".join(str(g.get("text", "")) for g in self.goals))
        for n, w in enumerate(self.widgets[:2], 1):  # what is here or held that a goal names: its state, opened
            for k, it in enumerate(w.items, 1):
                if it.eid and it.eid not in seen and words(self.names.name(it.eid)) & goals:
                    seen.add(it.eid)
                    out.append((f"{n}.{k} {self.names.name(it.eid)}", it.opens))
        notes = [it for it in self.widgets[[w.title.startswith("Your memory") for w in self.widgets].index(True)].items
                 if words(it.opens[1]) & self.asked] if any(w.title.startswith("Your memory") for w in self.widgets) \
            else []
        if notes:
            out.append(("your notes that share the words said to you",
                        ("text", "\n".join(f"- {it.opens[1]}" for it in notes[:6]))))
        return out[:4]
