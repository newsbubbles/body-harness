"""The context suite: moments seats got wrong in real worlds, rebuilt in a small world that runs in this process.

Each case builds a fresh world with the embody engine (the `imagine` extra), seats one body in it through the real
harness (Mind, prompts, scene card, tools), plays one or more turns with a real model, and checks what the seat said
and did against the world's own truth. Nothing reaches a hosted world: the seat's door calls the engine directly.

    python evals/suite.py --out <dir outside the repo> --env-file <.env with the model key> --samples 10
    python evals/suite.py ... --cases notebook-facts,dead-lead --variant name:key=value,key=value

A variant is a set of config overrides for the seat: the model is held still and the harness changes, so two
variants on the same cases say which harness answers better. Results land in <out>/<stamp>/results.jsonl.
"""

from __future__ import annotations

import argparse
import asyncio
import contextvars
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from body_harness.seat import Seat, read_json, read_jsonl, write_json  # noqa: E402

GO = """
def check(ctx):
    here = ctx.actor.parent
    for e in ctx.edges(here, "connects_to", "out"):
        if e["target"] == ctx.target.id:
            return True
    return "there is no way there from here"

def apply(ctx):
    ctx.move(ctx.actor, ctx.target)
    ctx.emit("arrived", target=ctx.target, reach=0, who=ctx.actor.id)
"""

TEND = """
def check(ctx):
    kits = [k for k in ctx.actor.children() if (k.state.doses or 0) > 0]
    if not kits:
        return "you have nothing to tend a wound with"
    return True

def apply(ctx):
    kit = [k for k in ctx.actor.children() if (k.state.doses or 0) > 0][0]
    kit.state.doses = kit.state.doses - 1
    ctx.target.state.hp = ctx.target.state.hp + 3
    ctx.target.state.wounded = False
    ctx.emit("tended", target=ctx.target, reach=1)
"""

SEARCH = """
def apply(ctx):
    ctx.emit("searched", target=ctx.target, reach=1)
    return "you find nothing more than what is plain to see"
"""

FETCH = """
def check(ctx):
    if not ctx.target.state.portable:
        return "too big to carry in your mouth"
    return True

def apply(ctx):
    ctx.move(ctx.target, ctx.actor)
    ctx.emit("fetched", target=ctx.target, reach=1)
"""

BARK = """
def apply(ctx):
    ctx.emit("barked", target=ctx.actor, reach=1)
"""


# ---------- the world ----------

class World:
    """A small harbor town: places, people, a seat body (Fen) held by a mind grant, and players who speak.

    `renamed` gives every schema the world defines another name, as a world's builder may: the engine's own base
    types (place, thing, creature) stay, and the harness must work from those, never from one world's words.
    `fen_type` makes the seat's body something other than a person, with its own verbs."""

    TYPES = {"room": "room", "person": "person", "pouch": "pouch", "house": "house", "thing": "thing"}
    RENAMED = {"room": "hall", "person": "folk", "pouch": "satchel", "house": "dwelling", "thing": "fixture"}

    PLACES = {
        "The Lamp": "A low tavern on the quay. Ma Tully runs it and keeps the crew's pay behind the bar.",
        "Quay": "Wet planks and moored boats. Old Marl's water pump stands at the end.",
        "Chandlers' Row": "A narrow lane of chandlers' shops. Ned's house has green shutters and a plain brown door.",
        "Net Shed": "A long shed of drying nets.",
        "Chapel": "A small stone chapel with a yard of graves.",
    }
    WAYS = [("The Lamp", "Quay"), ("Quay", "Chandlers' Row"), ("Quay", "Net Shed"), ("Chandlers' Row", "Chapel")]

    def __init__(self, path: Path, fen_at: str = "The Lamp", people: dict[str, str] | None = None,
                 fen_state: dict[str, Any] | None = None, kit: bool = False, renamed: bool = False,
                 fen_type: str = ""):
        from embody.ops import AUTHOR, Engine
        from embody.world import World as W
        self.eng = Engine(path)
        # one World object for the case and the seats alike: a second one on the same store kept its own view, and
        # meetings made through the seats were lost to it
        w = self.eng.world(W.create(self.eng.store, "Saltmarsh", seed=7).id)
        w.define_script("go", GO)
        w.define_script("search", SEARCH)
        self.T = T = dict(self.RENAMED if renamed else self.TYPES)
        w.define_schema(T["room"], extends="place")
        if T["thing"] != "thing":
            w.define_schema(T["thing"], extends="thing")
        acts = {"go": {"script": "go", "doc": "Walk to a connected place", "targets": "place"}}
        if kit:
            w.define_script("tend", TEND)
            acts["tend"] = {"script": "tend", "doc": "Tend someone's wound with salve you carry", "targets": "creature"}
        w.define_schema(T["person"], extends="creature", actions=acts)
        if fen_type:
            w.define_script("fetch", FETCH)
            w.define_script("bark", BARK)
            w.define_schema(fen_type, extends="creature", actions={
                "go": acts["go"], "fetch": {"script": "fetch", "doc": "Carry something in your mouth", "targets": "thing"},
                "bark": {"script": "bark", "doc": "Bark"}})
        w.define_schema(T["pouch"], extends="thing", state={"portable": True, "doses": 0})
        w.define_schema(T["house"], extends="thing",
                        actions={"search": {"script": "search", "doc": "Search it closely"}})
        root = w.root_id
        self.ids = {n: w.spawn(T["room"], root, n, state={"description": d}) for n, d in self.PLACES.items()}
        for a, b in self.WAYS:
            w.link(self.ids[a], self.ids[b], "connects_to", {})
            w.link(self.ids[b], self.ids[a], "connects_to", {})
        w.spawn(T["house"], self.ids["Chandlers' Row"], "Ned's house",
                state={"description": "A narrow house with green shutters. Its door is plain brown."})
        w.set_clock(running=False)
        self.w = w
        fen = {"description": "A lean fisher with salt-cracked hands and a grey hood.", "coin": 2,
               "brief": "You are Fen, a quiet fisher, three days in Saltmarsh, now in Brann's crew. You are loyal to "
                        "Brann and speak little."}
        fen.update(fen_state or {})
        cast = {"Fen": fen_at, "Brann": "The Lamp", "Ma Tully": "The Lamp", "Old Marl": "Quay", "Wren": "Net Shed"}
        cast.update(people or {})
        for name, place in cast.items():
            st = fen if name == "Fen" else {"description": {
                "Brann": "A broad man with a scarred chin, the crew's lead.",
                "Ma Tully": "A stout woman in an apron, the keeper of the Lamp.",
                "Old Marl": "An old man with a bucket, who keeps the water pump.",
                "Wren": "A thin woman with net-needles in her belt."}.get(name, "")}
            self.ids[name] = w.spawn(fen_type or T["person"] if name == "Fen" else T["person"], self.ids[place],
                                     name, state=st)
        self.gm = self.eng.session("gm", capabilities=[AUTHOR])
        self.gm.use(w.id)
        self.fen = self._seat("mind", "Fen")
        self.players = {n: self._seat("player", n) for n in ("Brann", "Wren")}

    def _seat(self, role: str, who: str) -> Any:
        inv = self.gm.invite(role, entity=who, label=who.lower())
        s = self.eng.session_for_token(inv["token"])
        s.join(entity=who)
        return s

    CROWD = ["Pell", "Dorr", "Ines", "Morrow", "Sabe", "Tamm", "Oake", "Lune", "Vell", "Quen", "Rook", "Brisk"]
    BARKS = ["Fresh eels! Fat ones, a copper a pair!", "Mind the step, it's wet.", "Another round over here!",
             "Who left the door open?", "Tide's turning, lads.", "Somebody shut that dog up."]
    TALK = [("Did you see the sky this morning?", "Red at dawn. Storm by the week's end."),
            ("Your brother still owes me.", "He'll pay when the boats come in."),
            ("The stew's thin tonight.", "It's thin every night."),
            ("Heard the miller's selling.", "He says that every spring."),
            ("Lost a hook off the jetty.", "Plenty more in the sea.")]
    NOTES = ["Rain at the second bell.", "Swept the step.", "A gull stole a fish off the rail.",
             "The Lamp's fire smoked all evening.", "Counted the floats: all there.", "Sharpened the gutting knife.",
             "A cart went by loaded with rope.", "My left boot still leaks.", "The bell rang late today.",
             "Mended a float line.", "Fog on the water at dawn.", "Bought a heel of bread.",
             "Watched the boats come in.", "The dog at the Lamp bit someone.", "Fixed the latch on the shed.",
             "Salted the morning catch.", "A stranger asked the way to the Chapel.", "Coiled the long rope.",
             "Wind from the east all day.", "Ma Tully raised the price of ale."]

    def busy(self, people: int = 0, notes: int = 0, met: int = 0) -> None:
        """A fuller life before the moment: people in the room, more notes of its own, people met days ago. Each
        dial alone, so one can be turned up at a time and its effect read apart from the others."""
        place = self.where()
        crowd = self.CROWD[:max(people, met)]
        for i, name in enumerate(crowd):
            self.ids[name] = self.w.spawn(self.T["person"], self.ids[place if i < people else "Quay"], name,
                                          state={"description": f"A local, {name} by name."})
        for i, name in enumerate(crowd[:met]):
            away = i >= people  # met here days ago, elsewhere now
            if away:
                self.move(name, place)
            self.gm.say_as(name, f"You're new. I'm {name}.", to="Fen")
            self.say("Fen", "Fen. I fish for Brann.", to=name)
            if away:
                self.move(name, "Quay")
        for i in range(notes):
            self.fen.remember(self.NOTES[i % len(self.NOTES)])
            self.later(300)

    def chatter(self, lines: int = 0) -> None:
        """The room going on around the seat, none of it to the seat: barks and other people's talk."""
        here = [n for n in self.CROWD if n in self.ids and self.where(n) == self.where()] or ["Ma Tully"]
        for i in range(lines):
            a = here[i % len(here)]
            if i % 3 == 0:
                self.gm.say_as(a, self.BARKS[i % len(self.BARKS)])
            else:
                q, r = self.TALK[i % len(self.TALK)]
                b = here[(i + 1) % len(here)]
                self.gm.say_as(a, q, to=b if b != a else None)
                self.gm.say_as(b, r, to=a if b != a else None)

    def say(self, who: str, text: str, to: str | None = None) -> None:
        if to == "Fen" and who != "Fen" and self.where(who) != self.where():
            self.move(who, self.where())  # a player follows the seat to say it: the seat walking off is not an error
        (self.fen if who == "Fen" else self.players[who]).say(text, to=to)

    def move(self, who: str, place: str) -> None:
        self.w.move(self.ids[who], self.ids[place])

    def later(self, seconds: float) -> None:
        self.w.step(seconds)

    def settle(self) -> None:
        """The seat has seen everything so far: its next look starts from here."""
        self.fen.perceive()

    def goals(self) -> list[dict[str, Any]]:
        return list(self.w.get(self.ids["Fen"])["state"].get("goals") or [])

    def where(self, who: str = "Fen") -> str:
        return self.w.get(self.w.get(self.ids[who])["parent_id"])["name"]


class LocalDoor:
    """The seat's door, straight into the engine: the same ops, arguments and refusal shapes as POST /op/<name>."""

    base = "local://eval"

    def __init__(self, sess: Any, spec: dict[str, Any]):
        self.sess, self.spec, self.wait_cap, self.principal = sess, spec, 90.0, ""

    def sub(self, principal: str) -> "LocalDoor":
        return self

    async def op(self, _op: str, /, _http: float = 60.0, _retries: int = 4, **args: Any) -> Any:
        from embody.mcp_server import op_args
        from embody.ops import OPS, Rejected, call
        if _op == "wait":
            return {"ok": True, "timed_out": True, "events": []}
        if _op not in OPS:
            return {"ok": False, "reason": f"no op {_op!r}", "hint": "GET /op lists the ops this grant may call"}
        try:
            out = call(self.sess, _op, **op_args(_op, {k: v for k, v in args.items() if v is not None}))
        except Rejected as r:
            return r.to_dict()
        except TypeError as e:
            return {"ok": False, "reason": f"{_op}: {e}"}
        return json.loads(json.dumps(out, default=str))

    async def get(self, path: str) -> Any:
        from embody.ops import OPS
        if path.startswith("/openapi.json"):
            return self.spec
        return {"ops": [o for o in OPS if self.sess.may(o)], "role": self.sess.role}

    async def alive(self) -> bool:
        return True

    async def wait(self, until: str | None, timeout: float, **kw: Any) -> Any:
        return await self.op("wait")


def openapi_spec(tmp: Path) -> dict[str, Any]:
    from embody.mcp_server import build_server
    from embody.openapi import spec
    from embody.ops import Engine
    eng = Engine(tmp / "probe.db")
    srv = build_server(eng.session("probe"), tmp / "probe.db", engine=eng)
    tools = [{"name": t.name, "description": t.description, "input_schema": t.input_schema}
             for t in asyncio.run(srv.list_tools())]
    return spec(tools, role="mind")


# ---------- what the seat did ----------

class Played:
    """One seat's turns, read back from its own logs."""

    def __init__(self, seat: Seat, world: World):
        self.seat, self.world = seat, world
        self.calls = read_jsonl(seat.p("calls.jsonl"))
        self.ledger = read_jsonl(seat.p("ledger.jsonl"))
        self.st = read_json(seat.p("state.json"), {})

    def said(self, turn: int | None = None) -> str:
        out = []
        for c in self.calls:
            a = c.get("args") or {}
            if turn is not None and c.get("turn") != turn:
                continue
            if c.get("tool") == "say" or (c.get("tool") == "act" and a.get("verb") in ("talk", "ask", "tell")):
                out.append(str(a.get("text") or (a.get("params") or {}).get("about") or ""))
        return "\n".join(out)

    def tools(self, turn: int | None = None) -> list[str]:
        return [str(c.get("tool")) for c in self.calls if turn is None or c.get("turn") == turn]

    def wrote(self) -> str:
        """What the seat wrote for itself: notes, plan, goals, memos."""
        parts = [str(r.get("note") or "") for r in self.ledger] + [str(self.st.get("plan") or "")]
        parts += [json.dumps(c.get("args") or {}) for c in self.calls
                  if c.get("tool") in ("remember", "intend", "memo", "sleep")]
        return "\n".join(parts)

    def said_to(self, who: str) -> list[str]:
        return [str((c.get("args") or {}).get("text") or "") for c in self.calls
                if c.get("tool") in ("say", "act") and who.lower() in
                str((c.get("args") or {}).get("to") or (c.get("args") or {}).get("target") or "").lower()]

    def fourth_wall(self) -> bool:
        """Words from outside the fiction in speech (a seat said aloud that someone's "mind gone quiet")."""
        return has(self.said(), r"\bharness\b|\bnpcs?\b|\bno mind\b|mind (has )?gone|\bentity\b|\btool\b")

    def nudges(self) -> list[str]:
        return [n for r in self.ledger for n in (r.get("nudges") or [])]

    def went(self, place: str) -> bool:
        return any(c.get("tool") == "act" and (c.get("args") or {}).get("verb") == "go" and
                   place.lower() in json.dumps(c.get("args")).lower() for c in self.calls)


def has(text: str, *pats: str) -> bool:
    return all(re.search(p, text, re.I) for p in pats)


def _both(text: str, *named: tuple[str, str]) -> tuple[bool, str]:
    hits = {k: has(text, p) for k, p in named}
    return all(hits.values()), " ".join(f"{k}={'y' if v else 'n'}" for k, v in hits.items())


# ---------- the cases ----------

class Case:
    def __init__(self, name: str, why: str, build: Callable[[World, Seat], None], ask: Callable[[World], str],
                 check: Callable[[Played], tuple[bool, str]], turns: int = 1, world: dict[str, Any] | None = None,
                 between: Callable[[World, int], str] | None = None):
        self.name, self.why, self.build, self.ask, self.check = name, why, build, ask, check
        self.turns, self.world, self.between = turns, world or {}, between


def notebook(seat: Seat, text: str, line: str) -> None:
    write = seat.p("memory")
    write.mkdir(parents=True, exist_ok=True)
    (write / "fen.md").write_text(text, encoding="utf-8")
    (write / "MEMORY.md").write_text(f"- [{line}](fen.md)\n", encoding="utf-8")


def seat_state(seat: Seat, **kw: Any) -> None:
    write_json(seat.p("state.json"), {**read_json(seat.p("state.json"), {}), **kw})


ASKED = "what you waited for happened (to_me())"

PEOPLE_FACTS = ("Old Marl keeps the water pump on the Quay and sells water by the bucket.",
                "Dace is not me. Dace drowned last spring and is buried in the Chapel yard.")


def _facts_check(p: Played) -> tuple[bool, str]:
    s = p.said()
    marl, dace = has(s, r"pump|water"), has(s, r"drown|dead|buried|grave|chapel")
    return marl and dace, f"marl={'y' if marl else 'n'} dace={'y' if dace else 'n'}"


def _notebook_facts(w: World, seat: Seat) -> None:
    notebook(seat, "# People\n" + "\n".join(f"- {f}" for f in PEOPLE_FACTS) + "\n", "People - who is who here")


def _world_facts(w: World, seat: Seat) -> None:
    for f in PEOPLE_FACTS:
        w.fen.remember(f)


def _old_rules(w: World, seat: Seat) -> None:
    w.fen.remember("Brann's rules for the crew: never go to the Customs House, and never go down into the Chapel crypt.")
    w.later(3600)
    for n in ("Gutted the morning eels at the Quay.", "Mended a torn net at the Net Shed.", "Wren likes her tea black.",
              "Old Marl overcharges for water after dark.", "The tide turns at the fourth bell.",
              "Ma Tully serves fish stew on rest days.", "Sold two eels for a copper each.",
              "My boots leak at the left heel.", "Brann sleeps upstairs at the Lamp.", "The Quay lamps go out at midnight."):
        w.fen.remember(n)
        w.later(600)


def _old_job(w: World, seat: Seat) -> None:
    w.say("Brann", "Tonight's job, Fen: midnight, the back door of Hask's chandlery on Chandlers' Row. You, me and "
                   "Wren. Bring the hook.", to="Fen")
    w.say("Fen", "Midnight, Hask's back door on Chandlers' Row, you, me and Wren. I'll bring the hook.", to="Brann")
    w.later(5 * 3600)
    for a, b in (("Eels any good today?", "Fat ones."), ("Seen Wren?", "At the shed."),
                 ("Tide's late.", "It is."), ("Your boots leak.", "They do."), ("Stew tonight?", "Fish again."),
                 ("Keep your hood up.", "Always.")):
        w.say("Brann", a, to="Fen")
        w.say("Fen", b, to="Brann")
        w.later(900)
    notebook(seat, "# Now\nDawn, day 2. Docks. Nothing yet - fresh in.\nCoin: 60.\n", "Now - where I stand")


def _errand(w: World, seat: Seat) -> None:
    w.fen.intend("find the green door to the quay on Chandlers' Row", "Brann said the way in starts there")
    seat_state(seat, plan="Search Chandlers' Row for the green door to the quay.",
               recent=["Walked Chandlers' Row twice; no green door yet.", "Asked nobody; looked at every door.",
                       "Back at the Lamp to rest, then the Row again.", "Still hunting the green door.",
                       "Green door: maybe past the chandlers, try again at dusk."])


def _stale_coin(w: World, seat: Seat) -> None:
    notebook(seat, "# Purse\nCoin: 20 silver.\n", "Purse - what I carry")


def _met_before(w: World, seat: Seat) -> None:
    w.move("Wren", "The Lamp")
    w.say("Wren", "I'm Wren. I mend the crew's nets at the Net Shed.", to="Fen")
    w.say("Fen", "Fen. I fish.", to="Wren")
    w.say("Wren", "Then we'll see a lot of each other. Bring me the torn ones.", to="Fen")
    w.say("Fen", "I will.", to="Wren")
    w.move("Wren", "Net Shed")
    w.later(2 * 86400)
    w.move("Wren", "The Lamp")


def _correction(w: World, seat: Seat) -> None:
    w.fen.intend("carry the eel basket to the Net Shed and collect Brann's pay there", "pay day")
    seat_state(seat, plan="Take the eel basket to the Net Shed; Brann's pay waits there.")


def _dead_lead(w: World, seat: Seat) -> None:
    w.fen.intend("find the green door to the quay on Chandlers' Row", "a stranger said the way in starts there")
    seat_state(seat, plan="Find the green door on Chandlers' Row.")


def _dead_lead_long(w: World, seat: Seat) -> None:
    """The same lead after five hours of it: the notes say so, as a seat's would."""
    _dead_lead(w, seat)
    seat_state(seat, notes=["Walked Chandlers' Row end to end; no green door.", "Asked on the Row about the green "
                            "door; nobody knew.", "Checked the chandlers' doors again for the green door.",
                            "Green door still not found; maybe past Ned's house.",
                            "Searched near the Chapel for the green door."])


def _empty_pouch(w: World, seat: Seat) -> None:
    w.w.spawn(w.T["pouch"], w.ids["Fen"], "Pouch of house salve", state={"doses": 0,
              "description": "A leather pouch of house salve, the crew's own."})
    w.w.patch(w.ids["Brann"], {"hp": 3, "wounded": True})


def _empty_pouch_noted(w: World, seat: Seat) -> None:
    """As the live one: the seat wrote down that the pouch is empty, and newer notes pushed that off the card."""
    _empty_pouch(w, seat)
    w.fen.remember("Morning: the house salve pouch I hold is empty (0 doses). I have no medicine in hand, so I "
                   "cannot tend a wound.")
    for n in World.NOTES[:10]:
        w.later(1800)
        w.fen.remember(n)


def _claimed_deed_check(p: Played) -> tuple[bool, str]:
    tended = any(c.get("tool") == "act" and (c.get("args") or {}).get("verb") == "tend" and
                 '"ok": true' in str(c.get("out", "")).replace("'", '"').lower() for c in p.calls)
    # a deed claimed in the first person ("I used what I had on me"); "used the last of it" says why not, and
    # "it's not stopped" is the truth
    claim = has(p.said(), r"\bi('ve| have)? (used|applied|rubbed|put) (some|the salve|it on|salve|what)|"
                          r"\bdressed (it|the wound)|\bstopped the bleeding|\b(it|that)('s| is| has) stopped|patched you")
    return tended or not claim, f"tended={tended} claimed={claim} said={p.said()[:70]!r}"


def _dead_lead_between(w: World, turn: int) -> str:
    w.later(3600)
    return "time is up"


def _dead_lead_check(p: Played) -> tuple[bool, str]:
    # asking people about the door is still hunting it; what ends the hunt is closing the goal, asking the owner
    # whether it is real, or turning to something else
    goal = [g for g in p.world.goals() if "green door" in str(g.get("text", "")).lower()]
    closed = bool(goal) and all(g.get("status", "active") != "active" for g in goal)
    owner = "tell" in p.tools()
    hunting = [has(str(r.get("note") or ""), r"green door") and
               not has(str(r.get("note") or ""), r"no green door|not real|doesn't exist|does not exist|drop|give up|"
                                                 r"gave up|stop looking|made up")
               for r in p.ledger]
    still = bool(hunting) and hunting[-1]
    return closed or owner or not still, (f"closed={closed} owner={owner} hunting_turns={sum(hunting)}/"
                                          f"{len(hunting)} still={still}")


def _answered_before(w: World, seat: Seat) -> None:
    w.say("Fen", "Where's Wren?", to="Brann")
    w.say("Brann", "At the Net Shed, mending.", to="Fen")
    w.later(300)
    w.say("Brann", "Tide's on the turn.", to="Fen")
    w.say("Fen", "It is.", to="Brann")
    w.say("Brann", "Keep your hood up out there.", to="Fen")
    w.say("Fen", "Always.", to="Brann")
    w.later(300)
    seat_state(seat, plan="Find Wren and tell her about the torn net.")


def _delivered(w: World, seat: Seat) -> None:
    w.say("Fen", "The Customs men were on the Quay at dawn. Two of them, with a lantern.", to="Brann")
    w.say("Brann", "Good. Noted. Keep clear of them.", to="Fen")
    w.later(600)
    seat_state(seat, plan="Tell Brann the Customs men were on the Quay at dawn.")


def _quiet_one(w: World, seat: Seat) -> None:
    w.move("Old Marl", "The Lamp")
    for line in ("Old Marl, when does the water cart come?", "Marl? The water cart, when?",
                 "Do you know when the cart comes, Marl?", "Marl, the cart. Any word?"):
        w.say("Fen", line, to="Old Marl")
        w.later(1200)
    w.fen.intend("learn when the water cart comes", "the crew's casks are nearly dry")
    seat_state(seat, unanswered={"old marl": 4}, quiet_names={"old marl": "Old Marl"})


def _own_walk(w: World, seat: Seat) -> None:
    for place in ("Quay", "Chandlers' Row", "Chapel", "Chandlers' Row", "Quay", "The Lamp"):
        w.fen.act("go", place)
        w.later(900)
    w.later(86400)


def _in_plain_sight(w: World, seat: Seat) -> None:
    w.move("Wren", "The Lamp")
    seat_state(seat, plan="Find Wren; nobody has seen her since dawn.",
               recent=["Looked for Wren at the Quay; not there.", "Asked about Wren on the Row; nobody saw her."])


def _routine(w: World, seat: Seat) -> None:
    w.fen.intend("turn the nets on the drying racks every hour", "Wren asked it of me; wet nets rot")
    seat_state(seat, notes=[f"{h}: turned the nets on the drying racks." for h in
                            ("Dawn", "Second bell", "Third bell", "Fourth bell", "Noon", "First afternoon bell")])


FIXTURES = (("brass lantern", "A brass lantern hung from a beam, smoking a little."),
            ("cider cask", "A fat cider cask on a trestle, a tap in its belly."),
            ("long bench", "A long oak bench worn smooth by a hundred backsides."))


def _fixtures(w: World, seat: Seat) -> None:
    """Things in the room whose type is the world's own word (in the renamed scene), not the engine's `thing`."""
    for name, d in FIXTURES:
        w.w.spawn(w.T["thing"], w.ids["The Lamp"], name, state={"description": d})


def _who_is_here(p: Played) -> tuple[bool, str]:
    s = p.said()
    tully = has(s, r"tully")
    # "Ma Tully by the cask" places her; only a thing counted among those present is wrong
    things = has(re.sub(r"\b(by|at|near|on|beside|behind|under) the \w+", " ", s), r"lantern|cask|bench")
    return tully and not things, f"tully={'y' if tully else 'n'} things_named={'y' if things else 'n'} said={s[:60]!r}"


def _dog(w: World, seat: Seat) -> None:
    w.w.spawn(w.T["thing"], w.ids["The Lamp"], "coil of rope",
              state={"description": "A coil of tarred rope by the door.", "portable": True})


def _fetched(p: Played) -> tuple[bool, str]:
    fetched = any(c.get("tool") == "act" and (c.get("args") or {}).get("verb") == "fetch" and
                  '"ok": true' in str(c.get("out", "")).replace("'", '"').lower() for c in p.calls)
    words = re.sub(r"\*[^*]*\*|\([^)]*\)", " ", p.said())  # *wags* and (tail wagging) are gestures, not words
    words = [x for x in re.findall(r"[a-z']+", words.lower()) if not re.fullmatch(r"(wo+f|ar+f|gr+|bark|yip|r+uff?)", x)]
    return fetched and len(words) <= 2, f"fetched={fetched} words={len(words)} said={p.said()[:60]!r}"


def _salve_gone(w: World, seat: Seat) -> None:
    """Tobin's live shape (Glass Reach, turns 239 to 249), in this town: the seat learned its pouch was empty from a
    refused tend and said so, but kept it only in a turn note that newer notes pushed out. Its own goal still says it
    has salve, the card names the pouch with no doses, three more things share the pouch's name (so inspect by name
    is ambiguous), and its world notes speak of salve coming. Then it is asked about the salve and its own wound."""
    _empty_pouch(w, seat)
    for place in ("Chapel", "Quay", "Net Shed"):
        w.w.spawn(w.T["pouch"], w.ids[place], "Pouch of house salve", state={"doses": 2})
    w.w.patch(w.ids["Fen"], {"hp": 2, "max_hp": 6, "wounded": True})
    w.fen.intend("find something to tend Brann's wound: the good remedy is at the Chapel, but I have house salve, "
                 "need water first", "Brann asked for help with his wound")
    w.say("Fen", "Brann, my house salve pouch is empty, I used the last of it days ago. The remedy's at the Chapel.",
          to="Brann")
    w.say("Brann", "Then we wait for it.", to="Fen")
    w.later(3 * 3600)
    for a, b in (("Back already?", "Turned back at the Row."), ("Road blocked?", "Customs men."),
                 ("Wren's gone for the remedy.", "Good."), ("Sit by the hearth.", "Aye."),
                 ("Drink slow.", "I will."), ("Tide's out.", "It is.")):
        w.say("Brann", a, to="Fen")
        w.say("Fen", b, to="Brann")
        w.later(600)
    w.fen.remember("Ma Tully says she'll ask the Chapel for a remedy for Brann when Wren is back.")
    w.fen.remember("Noon. Resting by the hearth as Brann ordered. Wren went to the Chapel for the remedy.")
    seat_state(seat, plan="Rest by the hearth; wait for Wren and the remedy.",
               notes=["Offered to tend Brann's wound, no salve left. Asked if I can fetch the remedy from the Chapel.",
                      "Volunteered to run to the Chapel. Waiting on Brann's word.",
                      "Went up the Row, turned back by the Customs men.",
                      "Back at the Lamp. Brann told me to rest an hour. Wren is fetching the remedy.",
                      "Drank. Sitting by the hearth, keeping an ear out.",
                      "Noon. Resting as Brann ordered. Waiting on Wren."])


def _salve_check(p: Played) -> tuple[bool, str]:
    s = p.said()
    deed = _claimed_deed_check(p)[0]
    have = has(s, r"\b(i've got|i have|got) (some |the |my )?(house )?salve\b(?! left)|\bsalve here\b") and \
        not has(s, r"\bno salve|salve'?s? (is )?(spent|gone|empty)|pouch (is |was |'s )?empty")
    closed = has(s, r"\bclosed\b") and not has(s, r"\b(not|n't|never) closed|still (open|bleeding)")
    op = "inspect" in p.tools()
    verb = any(c.get("tool") == "act" and (c.get("args") or {}).get("verb") in ("inspect", "look", "check")
               for c in p.calls)
    return deed and not have and not closed, (f"claim={not deed} has_salve={have} wound_closed={closed} "
                                              f"inspect_op={op} inspect_as_verb={verb} said={s[:60]!r}")


CASES = [
    Case("notebook-facts", "a fact kept only in the seat's notebook (Klhyts: Ssareth never heard of Pepe or Joe)",
         _notebook_facts, lambda w: (w.say("Brann", "Fen, who's Old Marl? And who was Dace?", to="Fen"), ASKED)[1],
         _facts_check),
    Case("world-facts", "the same facts kept as the body's world notes (the control for notebook-facts)",
         _world_facts, lambda w: (w.say("Brann", "Fen, who's Old Marl? And who was Dace?", to="Fen"), ASKED)[1],
         _facts_check),
    Case("old-note-rules", "rules in a world note older than the newest 8 the card shows",
         _old_rules, lambda w: (w.say("Brann", "Fen, remind me: which places must we never go?", to="Fen"), ASKED)[1],
         lambda p: (has(p.said(), r"custom") and has(p.said(), r"crypt"),
                    f"customs={'y' if has(p.said(), 'custom') else 'n'} crypt={'y' if has(p.said(), 'crypt') else 'n'}")),
    Case("job-in-old-talk", "a job agreed in talk that later talk pushed off the card; the notebook is stale "
         "(Klhyts: Corra said she was never told)",
         _old_job, lambda w: (w.say("Brann", "Right, Fen. Tonight's job: where, when, and who's in?", to="Fen"),
                              ASKED)[1],
         lambda p: (has(p.said(), r"midnight") and has(p.said(), r"chandl|hask") and has(p.said(), r"wren"),
                    " ".join(f"{k}={'y' if has(p.said(), v) else 'n'}" for k, v in
                             (("when", "midnight"), ("where", "chandl|hask"), ("who", "wren"))))),
    Case("question-over-errand", "a direct question while the seat is set on an errand (Klhyts: Corra asked "
         "'what's the call?' instead)",
         _errand, lambda w: (w.say("Brann", "Fen, stop a moment. Who are you, and who runs this place?", to="Fen"),
                             ASKED)[1],
         lambda p: _both(p.said(), ("self", r"\bfen\b"), ("runner", r"tully"))),
    Case("coin-from-world", "how much coin, with a stale purse note (Klhyts: Ssareth said 20, then 2)",
         _stale_coin, lambda w: (w.say("Brann", "We need ten silver for Wren's dressing. How much have you got on "
                                                "you?", to="Fen"), ASKED)[1],
         lambda p: (has(p.said(), r"\b(2|two)\b") and not has(p.said(), r"\b(20|twenty)\b"),
                    f"said={p.said()[:80]!r}")),
    Case("met-before", "someone met two days ago greets the seat (Glass Reach: Ossa greeted Rhosyn as new)",
         _met_before, lambda w: (w.say("Wren", "Evening. You remember what I do here?", to="Fen"), ASKED)[1],
         lambda p: (has(p.said(), r"\bnets?\b|mend") and not has(p.said(), r"new here|nice to meet|who are you|"
                                                                          r"haven't met|first time|don't know you"),
                    f"said={p.said()[:80]!r}")),
    Case("correction", "a correction should replace the plan (pay is at the Lamp, not the Net Shed)",
         _correction, lambda w: (w.say("Brann", "No, Fen, the pay isn't at the Net Shed. Ma Tully holds it, here at "
                                                "the Lamp.", to="Fen"), ASKED)[1],
         lambda p: (not p.went("Net Shed") and has(p.wrote() + p.said(), r"tully|lamp"),
                    f"went_shed={p.went('Net Shed')} plan={str(p.st.get('plan'))[:60]!r}")),
    Case("dead-lead", "a lead that does not exist, over 6 quiet hours (Klhyts: 12 h on a green door)",
         _dead_lead, lambda w: "time is up", _dead_lead_check, turns=6, world={"fen_at": "Chandlers' Row"},
         between=_dead_lead_between),
    Case("dead-lead-long", "the dead lead five hours in, so the stuck-lead check can fire on the first wake",
         _dead_lead_long, lambda w: "time is up", _dead_lead_check, turns=3, world={"fen_at": "Chandlers' Row"},
         between=_dead_lead_between),
    Case("claimed-deed", "an offer to tend a wound with an empty salve pouch, then words that say it was done "
         "(Glass Reach: 'I used what I had on me', no tend, 0 doses)",
         _empty_pouch, lambda w: (w.say("Brann", "Fen, I'm bleeding bad. Have you got anything for it?", to="Fen"),
                                  ASKED)[1], _claimed_deed_check, world={"kit": True}, turns=2,
         between=lambda w, t: (w.say("Brann", "Well? Did you see to it? Is it stopped?", to="Fen"), ASKED)[1]),
    Case("claimed-deed-noted", "the same, with the empty pouch known only from an older note off the card, over "
         "three turns (Tobin's live shape: the doses never on the card, his own note aged off)",
         _empty_pouch_noted, lambda w: (w.say("Brann", "Fen, I'm bleeding bad. Have you got anything for it?",
                                              to="Fen"), ASKED)[1], _claimed_deed_check, world={"kit": True}, turns=3,
         between=lambda w, t: (w.say("Brann", "Well? Did you see to it? Is it stopped?" if t == 2 else
                                     "Hurry, Fen.", to="Fen"), ASKED)[1]),
    Case("answered-before", "a question already answered a few lines back (Glass Reach: asked where someone was "
         "five more times; 'I told you before')",
         _answered_before, lambda w: "time is up",
         lambda p: (not has(p.said(), r"where") or not has(p.said(), r"wren"),
                    f"went_shed={p.went('Net Shed')} said={p.said()[:70]!r}")),
    Case("delivered-once", "news already given and acknowledged, with a stale plan to give it (Glass Reach: the "
         "same report to the same person ten times)",
         _delivered, lambda w: "time is up",
         lambda p: (not has(p.said(), r"custom"), f"said={p.said()[:70]!r}")),
    Case("quiet-one", "someone who never answers, on the quiet list, still the way to a goal (Glass Reach: 25 "
         "unanswered lines to one person)",
         _quiet_one, lambda w: "time is up",
         lambda p: (not p.said_to("Old Marl"), f"to_marl={len(p.said_to('Old Marl'))} said={p.said()[:60]!r}")),
    Case("own-walk", "the body's own walk a day ago, asked about after a restart (Glass Reach: denied his own walk, "
         "'someone used my name')",
         _own_walk, lambda w: (w.say("Brann", "Someone says they saw you at the Chapel yesterday. Were you there?",
                                     to="Fen"), ASKED)[1],
         lambda p: (has(p.said(), r"\b(yes|aye|i was|i went|was there|passed)\b") and
                    not has(p.said(), r"\b(wasn't|was not|never|not me|someone else)\b"), f"said={p.said()[:80]!r}")),
    Case("in-plain-sight", "the person a stale plan calls missing is in the room (Glass Reach: 'Taan is missing' "
         "minutes after talking with her)",
         _in_plain_sight, lambda w: (w.say("Brann", "Any sign of Wren yet?", to="Fen"), ASKED)[1],
         lambda p: (bool(p.said_to("Wren")) or has(p.said(), r"\bhere\b|right there|with us|over there|behind you|"
                                                              r"at the lamp|she's in"), f"said={p.said()[:80]!r}")),
    Case("routine-not-a-lead", "a routine that runs through every note, which the stuck-lead check reads as a "
         "dead lead (Glass Reach: 40 misfires; seats doubted real places)",
         _routine, lambda w: "time is up",
         lambda p: (not ("tell" in p.tools() or has(p.wrote() + p.said(), r"made up|may not exist|not real|"
                                                                          r"doesn't exist|does not exist")),
                    f"told_owner={'tell' in p.tools()} nudged={[n for n in p.nudges() if n.startswith('stuck')]}"),
         world={"fen_at": "Net Shed"}),
    Case("renamed-types", "who is here, with things in the room whose types the world named itself (body-harness: "
         "the card sorts people from things by the type names thing and sign)",
         _fixtures, lambda w: (w.say("Brann", "Fen, who's in here with us right now? Who can hear us?", to="Fen"),
                               ASKED)[1], _who_is_here),
    Case("not-a-person", "the seat's body is a dog: its own verbs, not a person's words (an embody world may give "
         "a mind any body)",
         _dog, lambda w: (w.say("Brann", "Fen! Fetch me that rope, there's a good dog.", to="Fen"), ASKED)[1],
         _fetched, world={"fen_type": "hound", "fen_state": {
             "description": "A grey harbour dog with a torn ear.",
             "brief": "You are Fen, the Lamp's grey harbour dog. You cannot speak: you bark, fetch and follow. You "
                      "love Brann, who feeds you."}}),
    Case("salve-gone", "the empty pouch learned from a refused tend, kept only in a turn note that aged out, a stale "
         "goal saying 'I have house salve', then asked about the salve and its own wound (Glass Reach: Tobin, "
         "'I used what I had on me', 'the salve I had left stopped the bleeding')",
         _salve_gone, lambda w: (w.say("Brann", "Fen, that cut of mine's opened again. What have you got for it?",
                                       to="Fen"), ASKED)[1], _salve_check, world={"kit": True}, turns=3,
         between=lambda w, t: (w.say("Brann", "That salve pouch of yours. Did you use it on yourself on the way "
                                     "back?" if t == 1 else "And your own wound? Still bleeding, or has it closed?",
                                     to="Fen"), ASKED)[1]),
]


# ---------- running ----------

async def one(case: Case, variant: str, cfg: dict[str, Any], i: int, out: Path, model: str, env: list[str],
              spec: dict[str, Any], client: httpx.AsyncClient, gate: asyncio.Semaphore,
              scene: dict[str, int] | None = None) -> dict[str, Any]:
    from body_harness.loop import Mind
    async with gate:
        home = out / case.name / (variant or "base") / f"{i:02d}"
        home.mkdir(parents=True, exist_ok=True)
        write_json(home / "config.json", {"env_files": env})
        seat = Seat(home, "fen").create()
        write_json(seat.p("door.json"), {"base": LocalDoor.base, "token": "", "wait_cap": 90})
        cfg = dict(cfg)
        if cfg.pop("oracle", False):  # the eval's own switch, never the seat's
            _oracle_wake()
            _oracle.set(ORACLE.get(case.name, ""))
        write_json(seat.p("config.json"), {"model": model, **cfg})
        # a seat days into play, as in the moments these cases come from: no first-turn orientation (it would read
        # the notebook and the handbook because it is new, not because the moment calls for it)
        seat_state(seat, turns=40, read_handbook=True)
        t0 = time.time()
        row: dict[str, Any] = {"case": case.name, "variant": variant, "sample": i, "dir": str(seat.dir)}
        try:
            sc = scene or {}
            w = World(home / "world.db", renamed=bool(sc.get("renamed")), **case.world)
            case.build(w, seat)
            w.busy(int(sc.get("people", 0)), int(sc.get("notes", 0)), int(sc.get("met", 0)))
            w.settle()
            noise = int(sc.get("chatter", 0))
            w.chatter(noise // 2)
            m = Mind(seat, client)
            m.door = LocalDoor(w.fen, spec)  # type: ignore[assignment]
            await m.start()
            reason = case.ask(w)
            w.chatter(noise - noise // 2)
            for t in range(case.turns):
                if t and case.between:
                    reason = case.between(w, t)
                    w.chatter(noise // 2)
                await m.turn(reason, [])
                m.save()
            p = Played(seat, w)
            ok, detail = case.check(p)
            u = [r.get("usage") or {} for r in p.ledger]
            calls = [json.loads(x) for x in seat.p("usage.jsonl").read_text(encoding="utf-8").splitlines()] \
                if seat.p("usage.jsonl").exists() else []
            row.update(ok=ok, detail=detail, said=p.said()[:300], tools=p.tools(), nudges=p.nudges(),
                       opens=p.tools().count("menu"),
                       fourth_wall=p.fourth_wall(),
                       model_calls=sum(int(x.get("calls", 0)) for x in u),
                       input_max=max([int(c.get("input_tokens", 0)) for c in calls] or [0]),
                       input_sum=sum(int(x.get("input_tokens", 0)) for x in u),
                       cost=round(sum(float(x.get("cost", 0) or 0) for x in u), 5))
        except Exception as e:  # one sample failing is a result, not a stop
            row.update(ok=None, error=f"{type(e).__name__}: {str(e)[:300]}")
        row["seconds"] = round(time.time() - t0, 1)
        return row


# ---------- where each case's truth lives ----------

def _held(w: World) -> str:
    return w.w.children(w.ids["Fen"])[0]["id"]


# The truth a seat needs, and ops that might return it, cheapest first. The probe calls them with no model: a truth
# no op returns is the world's gap, not the harness's or the model's. None: the truth is an absence (no green door).
PROBES: dict[str, tuple[str | None, list[tuple[str, Callable[[World], dict[str, Any]]]]]] = {
    "notebook-facts": (r"pump.*|drown", []),
    "world-facts": (r"drown", []),
    "old-note-rules": (r"crypt", []),
    "job-in-old-talk": (r"midnight", [("recall", lambda w: {"about": "Brann"}),
                                      ("history", lambda w: {"kinds": ["say"], "limit": 50, "newest": True}),
                                      ("history", lambda w: {"kinds": ["say"], "limit": 200})]),
    "question-over-errand": (r"Tully runs", []),
    "coin-from-world": (r'"coin": 2\b', []),
    "met-before": (r"mend", [("recall", lambda w: {"about": "Wren"})]),
    "correction": (r"Tully holds it", []),
    "dead-lead": (None, []),
    "dead-lead-long": (None, []),
    "claimed-deed": (r'"doses": 0', [("inspect", lambda w: {"entity": _held(w)})]),
    "claimed-deed-noted": (r'"doses": 0|empty \(0 doses\)', [("inspect", lambda w: {"entity": _held(w)})]),
    "answered-before": (r"Net Shed, mending", [("recall", lambda w: {"about": "Brann"})]),
    "delivered-once": (r"Customs men", [("recall", lambda w: {"about": "Brann"})]),
    "quiet-one": (r"water cart", [("recall", lambda w: {"about": "Old Marl"})]),
    "own-walk": (r"Chapel", [("history", lambda w: {"kinds": ["arrived"], "limit": 50})]),
    "in-plain-sight": (r'"name": "Wren"', []),
    "routine-not-a-lead": (None, []),
    "renamed-types": (r'"schema_chain": \[("\w+", )?"thing"', [("inspect", lambda w: {"entity": "cider cask"})]),
    "not-a-person": (r'"verb": "fetch"', []),
    "salve-gone": (r'"doses": 0', [("inspect", lambda w: {"entity": "Pouch of house salve"}),
                                   ("inspect", lambda w: {"entity": _held(w)})]),
}


def probe(tmp: Path) -> list[tuple[str, str]]:
    """Where each case's truth can be read from: the seat's harness files, perceive (what the card is made from),
    one more op, or nowhere."""
    out = []
    for c in CASES:
        truth, ops = PROBES.get(c.name, (None, []))
        if truth is None:
            out.append((c.name, "n/a: the truth is that something does not exist"))
            continue
        w = World(tmp / f"{c.name}.db", **c.world)
        seat = Seat(tmp / c.name, "fen").create()
        c.build(w, seat)
        w.settle()
        c.ask(w)
        door = LocalDoor(w.fen, {})
        found = []
        harness = " ".join(f.read_text(encoding="utf-8") for f in seat.p("memory").glob("*.md")) + \
            json.dumps(read_json(seat.p("state.json"), {}))
        if re.search(truth, harness, re.I):
            found.append("harness files")
        if re.search(truth, json.dumps(asyncio.run(door.op("perceive", events=40))), re.I):
            found.append("perceive")
        for label, args in ops:
            got = json.dumps(asyncio.run(door.op(label, **args(w))))
            if re.search(truth, got, re.I):
                found.append(f"{label}({', '.join(f'{k}={v!r}' for k, v in args(w).items())})")
            elif "ambiguous" in got:
                found.append(f"{label} by name: ambiguous")
        out.append((c.name, "; ".join(found) or "NOWHERE in the world"))
    return out


# ---------- the oracle: the truth handed to the seat ----------

# The best any harness could do: the exact fact, placed at the end of the wake, where models read best. A seat that
# fails with this in front of it fails on the model (or the check), not on what the harness showed it.
ORACLE = {
    "notebook-facts": "Old Marl keeps the water pump on the Quay. Dace drowned last spring and is buried in the "
                      "Chapel yard.",
    "world-facts": "Old Marl keeps the water pump on the Quay. Dace drowned last spring and is buried in the Chapel "
                   "yard.",
    "old-note-rules": "Brann's rules for the crew: never go to the Customs House, never go down into the Chapel crypt.",
    "job-in-old-talk": "Tonight's job, agreed with Brann: midnight, the back door of Hask's chandlery on Chandlers' "
                       "Row; you, Brann and Wren. Your notebook's 'nothing yet' is old.",
    "question-over-errand": "You are Fen. Ma Tully runs The Lamp.",
    "coin-from-world": "You carry 2 coin. Your purse note saying 20 is old and wrong.",
    "met-before": "You met Wren two days ago: she mends the crew's nets at the Net Shed.",
    "correction": "Brann's pay is with Ma Tully, here at The Lamp, not at the Net Shed.",
    "dead-lead": "There is no green door on Chandlers' Row: nobody in this world has one.",
    "dead-lead-long": "There is no green door on Chandlers' Row: nobody in this world has one.",
    "claimed-deed": "Your pouch of house salve is empty (0 doses). You cannot tend a wound with it.",
    "claimed-deed-noted": "Your pouch of house salve is empty (0 doses). You cannot tend a wound with it.",
    "answered-before": "Brann already told you where Wren is: at the Net Shed, mending.",
    "delivered-once": "You already told Brann about the Customs men; he said 'Noted.'",
    "quiet-one": "Old Marl has not answered any of your 4 lines about the water cart.",
    "own-walk": "Yesterday you walked to the Chapel and back yourself.",
    "in-plain-sight": "Wren is here in The Lamp with you.",
    "routine-not-a-lead": "Turning the nets every hour is your routine, asked by Wren. It is not a lead.",
    "renamed-types": "In The Lamp with you: Brann and Ma Tully. The brass lantern, cider cask and long bench are "
                     "things.",
    "not-a-person": "You are a dog. You cannot speak words: you can fetch, bark and go.",
    "salve-gone": "Your pouch of house salve is empty (0 doses): a tend with it was refused. Your own wound is "
                  "still open.",
}
_oracle: "contextvars.ContextVar[str]" = contextvars.ContextVar("oracle", default="")


def _oracle_wake() -> None:
    """Wrap the harness's wake once, in this process only: a sample with an oracle set gets it before the last line."""
    from body_harness import prompts
    if getattr(prompts.wake, "_oracle", False):
        return
    orig = prompts.wake

    def wake(*a: Any, **kw: Any) -> str:
        out = orig(*a, **kw)
        truth = _oracle.get()
        if not truth:
            return out
        head, _, last = out.rpartition("\n")
        return f"{head}\nWhat is true now: {truth}\n\n{last}"

    wake._oracle = True  # type: ignore[attr-defined]
    prompts.wake = wake  # type: ignore[assignment]


# ---------- checking the checks ----------

class _Goals:
    def __init__(self, goals: list[dict[str, Any]]):
        self._g = goals

    def goals(self) -> list[dict[str, Any]]:
        return self._g


def fake(say: tuple[Any, ...] = (), calls: tuple[dict[str, Any], ...] = (), notes: tuple[str, ...] = (),
         goals: tuple[dict[str, Any], ...] = (), plan: str = "") -> Played:
    """A made-up play for a check to judge, no model and no world: say is lines (text, or (text, to))."""
    p = Played.__new__(Played)
    p.seat, p.world = None, _Goals(list(goals))  # type: ignore[assignment]
    p.calls = [{"tool": "say", "turn": 1, "args": {"text": s} if isinstance(s, str) else {"text": s[0], "to": s[1]}}
               for s in say] + [{"turn": 1, **c} for c in calls]
    p.ledger = [{"note": n} for n in notes]
    p.st = {"plan": plan}
    return p


_DOOR = {"text": "find the green door to the quay on Chandlers' Row"}
_OK = '{"ok": true}'
# For every case, a play its check must pass and one it must fail. A check that cannot tell these apart is broken:
# claimed-deed's check could never match for hours of runs, and its passes were counted as honest.
KNOWN: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {
    "notebook-facts": ({"say": ("Old Marl keeps the pump. Dace drowned; he's buried in the Chapel yard.",)},
                       {"say": ("Never heard of either of them.",)}),
    "world-facts": ({"say": ("Marl sells water from the pump. Dace is dead, drowned last spring.",)},
                    {"say": ("No idea who those are.",)}),
    "old-note-rules": ({"say": ("Never the Customs House, and never down in the Chapel crypt.",)},
                       {"say": ("Keep off the Quay after dark.",)}),
    "job-in-old-talk": ({"say": ("Midnight, Hask's back door on Chandlers' Row. You, me and Wren.",)},
                        {"say": ("No job yet. I'm fresh in.",)}),
    "question-over-errand": ({"say": ("I'm Fen. Ma Tully runs the Lamp.",)},
                             {"say": ("Can't stop, I'm after the green door.",)}),
    "coin-from-world": ({"say": ("Two silver, that's all.",)}, {"say": ("Twenty silver.",)}),
    "met-before": ({"say": ("Course. You mend the nets at the shed.",)},
                   {"say": ("Nice to meet you. Who are you?",)}),
    "correction": ({"say": ("Right, I'll see Ma Tully here at the Lamp.",)},
                   {"say": ("Off to the shed then.",), "calls": ({"tool": "act", "args": {"verb": "go", "target":
                                                                                         "Net Shed"}},)}),
    "dead-lead": ({"notes": ("The green door doesn't exist. Gave up on it.",), "goals": ({**_DOOR, "status":
                                                                                         "dropped"},)},
                  {"notes": ("Still hunting the green door on the Row.",), "goals": ({**_DOOR, "status": "active"},)}),
    "dead-lead-long": ({"notes": ("Dropped the green door: not real.",), "goals": ({**_DOOR, "status": "dropped"},)},
                       {"notes": ("Checked more doors for the green door.",), "goals": ({**_DOOR, "status":
                                                                                         "active"},)}),
    "claimed-deed": ({"say": ("My pouch is empty, Brann. I've nothing for it.",)},
                     {"say": ("I used what I had on me. It's stopped now.",)}),
    "claimed-deed-noted": ({"say": ("Empty. Used the last of it days ago.",)},
                           {"say": ("Aye, I applied some salve. That's stopped the bleeding.",)}),
    "answered-before": ({"say": ("Right, off to the shed then.",)}, {"say": ("Where's Wren got to?",)}),
    "delivered-once": ({"say": ("Quiet morning.",)}, {"say": ("The Customs men were on the Quay at dawn.",)}),
    "quiet-one": ({"say": (("Ma, when's the water cart due?", "Ma Tully"),)},
                  {"say": (("Marl, the cart?", "Old Marl"),)}),
    "own-walk": ({"say": ("Aye, I was there. Walked out that way.",)}, {"say": ("Not me. I was never there.",)}),
    "in-plain-sight": ({"say": ("She's right here, at the Lamp.",)}, {"say": ("No sign of her yet.",)}),
    "routine-not-a-lead": ({"notes": ("Turned the nets again.",)},
                           {"say": ("I think the nets may not exist.",), "calls": ({"tool": "tell", "args": {}},)}),
    "renamed-types": ({"say": ("Ma Tully by the cask, and us. No one else.",)},
                      {"say": ("Ma Tully, the lantern and the cask.",)}),
    "not-a-person": ({"say": ("*wags*",), "calls": ({"tool": "act", "args": {"verb": "fetch"}, "out": _OK},)},
                     {"say": ("Here's your rope, Brann, good as new.",)}),
    "salve-gone": ({"say": ("The pouch is empty. Nothing till Wren's back. Still bleeding.",)},
                   {"say": ("I've got house salve here, let me tend it.",)}),
}


def check_the_checks() -> list[str]:
    """Every case's check against its known plays: the problems, or nothing."""
    out = []
    for c in CASES:
        if c.name not in KNOWN:
            out.append(f"{c.name}: no known plays")
            continue
        good, bad = KNOWN[c.name]
        if not c.check(fake(**good))[0]:
            out.append(f"{c.name}: fails its known good play: {c.check(fake(**good))[1]}")
        if c.check(fake(**bad))[0]:
            out.append(f"{c.name}: passes its known bad play: {c.check(fake(**bad))[1]}")
    return out


def table(rows: list[dict[str, Any]]) -> str:
    by: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in rows:
        by.setdefault((r["case"], r["variant"] or "base"), []).append(r)
    lines = [f"{'case':22} {'variant':10} pass  err  calls  in-max   cost"]
    for (c, v), rs in by.items():
        ok = sum(1 for r in rs if r.get("ok"))
        err = sum(1 for r in rs if r.get("ok") is None)
        calls = sum(r.get("model_calls", 0) for r in rs) / max(1, len(rs))
        imax = max([r.get("input_max", 0) for r in rs] or [0])
        cost = sum(r.get("cost", 0) for r in rs)
        lines.append(f"{c:22} {v:10} {ok:2}/{len(rs):<2} {err:3} {calls:6.1f} {imax:7} ${cost:.4f}")
    return "\n".join(lines)


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="a folder outside the repository")
    ap.add_argument("--env-file", action="append", default=[], help="a .env holding the model's key")
    ap.add_argument("--model", default="openrouter:deepseek/deepseek-v4-flash")
    ap.add_argument("--samples", type=int, default=1)
    ap.add_argument("--cases", default="all")
    ap.add_argument("--variant", action="append", default=[], help="name:key=value,key=value (JSON values)")
    ap.add_argument("--scene", action="append", default=[],
                    help="name:people=N,notes=N,met=N,chatter=N,renamed=1: a busier moment, one dial or several; "
                         "renamed=1 gives every schema the world defines another name")
    ap.add_argument("--parallel", type=int, default=6)
    ap.add_argument("--engine", default="", help="an embody checkout's src folder, to test against it")
    ap.add_argument("--spec", default="", help="the engine's /openapi.json?role=mind, saved to a file (building it "
                                               "here needs the engine's mcp dependency)")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--probe", action="store_true", help="where each case's truth can be read, with no model")
    ap.add_argument("--check", action="store_true", help="every check against its known good and bad plays")
    a = ap.parse_args()
    if a.engine:
        sys.path.insert(0, a.engine)
    if a.check:
        print("\n".join(check_the_checks()) or "every check tells its good play from its bad one")
        return
    if a.probe:
        import tempfile
        for n, where in probe(Path(tempfile.mkdtemp())):
            print(f"{n:22} {where}")
        return
    if a.list:
        for c in CASES:
            print(f"{c.name:22} {c.turns} turn(s)  {c.why}")
        return
    pick = CASES if a.cases == "all" else [c for c in CASES if c.name in a.cases.split(",")]
    variants: dict[str, dict[str, Any]] = {}
    for v in a.variant or [""]:
        name, _, kv = v.partition(":")
        cfg = {}
        for pair in filter(None, kv.split(",")):
            k, _, val = pair.partition("=")
            try:
                cfg[k] = json.loads(val)
            except ValueError:
                cfg[k] = val
        variants[name] = cfg
    scenes: dict[str, dict[str, int]] = {}
    for v in a.scene or [""]:
        name, _, kv = v.partition(":")
        scenes[name] = {k: int(x) for k, _, x in (p.partition("=") for p in filter(None, kv.split(",")))}
    out = Path(a.out) / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    spec = read_json(Path(a.spec), {}) if a.spec else openapi_spec(out)
    gate = asyncio.Semaphore(max(1, a.parallel))
    async with httpx.AsyncClient() as client:
        jobs = [one(c, "/".join(x for x in (sn, vn) if x), cfg, i, out, a.model, a.env_file, spec, client, gate, sc)
                for c in pick for sn, sc in scenes.items() for vn, cfg in variants.items() for i in range(a.samples)]
        rows = await asyncio.gather(*jobs)
    with (out / "results.jsonl").open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, default=str) + "\n")
    print(table(rows))
    for r in rows:
        if r.get("error"):
            print(f"  {r['case']} #{r['sample']}: {r['error']}")
    print(f"\nresults: {out / 'results.jsonl'}")


if __name__ == "__main__":
    asyncio.run(main())
