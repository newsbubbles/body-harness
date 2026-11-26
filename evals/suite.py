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

SEARCH = """
def apply(ctx):
    ctx.emit("searched", target=ctx.target, reach=1)
    return "you find nothing more than what is plain to see"
"""


# ---------- the world ----------

class World:
    """A small harbor town: places, people, a seat body (Fen) held by a mind grant, and players who speak."""

    PLACES = {
        "The Lamp": "A low tavern on the quay. Ma Tully runs it and keeps the crew's pay behind the bar.",
        "Quay": "Wet planks and moored boats. Old Marl's water pump stands at the end.",
        "Chandlers' Row": "A narrow lane of chandlers' shops. Ned's house has green shutters and a plain brown door.",
        "Net Shed": "A long shed of drying nets.",
        "Chapel": "A small stone chapel with a yard of graves.",
    }
    WAYS = [("The Lamp", "Quay"), ("Quay", "Chandlers' Row"), ("Quay", "Net Shed"), ("Chandlers' Row", "Chapel")]

    def __init__(self, path: Path, fen_at: str = "The Lamp", people: dict[str, str] | None = None,
                 fen_state: dict[str, Any] | None = None):
        from embody.ops import AUTHOR, Engine
        from embody.world import World as W
        self.eng = Engine(path)
        w = W.create(self.eng.store, "Saltmarsh", seed=7)
        w.define_script("go", GO)
        w.define_script("search", SEARCH)
        w.define_schema("room", extends="place")
        w.define_schema("person", extends="creature",
                        actions={"go": {"script": "go", "doc": "Walk to a connected place", "targets": "place"}})
        w.define_schema("house", extends="thing",
                        actions={"search": {"script": "search", "doc": "Search it closely"}})
        root = w.root_id
        self.ids = {n: w.spawn("room", root, n, state={"description": d}) for n, d in self.PLACES.items()}
        for a, b in self.WAYS:
            w.link(self.ids[a], self.ids[b], "connects_to", {})
            w.link(self.ids[b], self.ids[a], "connects_to", {})
        w.spawn("house", self.ids["Chandlers' Row"], "Ned's house",
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
            self.ids[name] = w.spawn("person", self.ids[place], name, state=st)
        self.gm = self.eng.session("gm", capabilities=[AUTHOR])
        self.gm.use(w.id)
        self.fen = self._seat("mind", "Fen")
        self.players = {n: self._seat("player", n) for n in ("Brann", "Wren")}

    def _seat(self, role: str, who: str) -> Any:
        inv = self.gm.invite(role, entity=who, label=who.lower())
        s = self.eng.session_for_token(inv["token"])
        s.join(entity=who)
        return s

    def say(self, who: str, text: str, to: str | None = None) -> None:
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
]


# ---------- running ----------

async def one(case: Case, variant: str, cfg: dict[str, Any], i: int, out: Path, model: str, env: list[str],
              spec: dict[str, Any], client: httpx.AsyncClient, gate: asyncio.Semaphore) -> dict[str, Any]:
    from body_harness.loop import Mind
    async with gate:
        home = out / case.name / (variant or "base") / f"{i:02d}"
        home.mkdir(parents=True, exist_ok=True)
        write_json(home / "config.json", {"env_files": env})
        seat = Seat(home, "fen").create()
        write_json(seat.p("door.json"), {"base": LocalDoor.base, "token": "", "wait_cap": 90})
        write_json(seat.p("config.json"), {"model": model, **cfg})
        # a seat days into play, as in the moments these cases come from: no first-turn orientation (it would read
        # the notebook and the handbook because it is new, not because the moment calls for it)
        seat_state(seat, turns=40, read_handbook=True)
        t0 = time.time()
        row: dict[str, Any] = {"case": case.name, "variant": variant, "sample": i, "dir": str(seat.dir)}
        try:
            w = World(home / "world.db", **case.world)
            case.build(w, seat)
            w.settle()
            m = Mind(seat, client)
            m.door = LocalDoor(w.fen, spec)  # type: ignore[assignment]
            await m.start()
            reason = case.ask(w)
            for t in range(case.turns):
                if t and case.between:
                    reason = case.between(w, t)
                await m.turn(reason, [])
                m.save()
            p = Played(seat, w)
            ok, detail = case.check(p)
            u = [r.get("usage") or {} for r in p.ledger]
            calls = [json.loads(x) for x in seat.p("usage.jsonl").read_text(encoding="utf-8").splitlines()] \
                if seat.p("usage.jsonl").exists() else []
            row.update(ok=ok, detail=detail, said=p.said()[:300], tools=p.tools(),
                       model_calls=sum(int(x.get("calls", 0)) for x in u),
                       input_max=max([int(c.get("input_tokens", 0)) for c in calls] or [0]),
                       input_sum=sum(int(x.get("input_tokens", 0)) for x in u),
                       cost=round(sum(float(x.get("cost", 0) or 0) for x in u), 5))
        except Exception as e:  # one sample failing is a result, not a stop
            row.update(ok=None, error=f"{type(e).__name__}: {str(e)[:300]}")
        row["seconds"] = round(time.time() - t0, 1)
        return row


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
    ap.add_argument("--parallel", type=int, default=6)
    ap.add_argument("--engine", default="", help="an embody checkout's src folder, to test against it")
    ap.add_argument("--spec", default="", help="the engine's /openapi.json?role=mind, saved to a file (building it "
                                               "here needs the engine's mcp dependency)")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    if a.engine:
        sys.path.insert(0, a.engine)
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
    out = Path(a.out) / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    spec = read_json(Path(a.spec), {}) if a.spec else openapi_spec(out)
    gate = asyncio.Semaphore(max(1, a.parallel))
    async with httpx.AsyncClient() as client:
        jobs = [one(c, vn, cfg, i, out, a.model, a.env_file, spec, client, gate)
                for c in pick for vn, cfg in variants.items() for i in range(a.samples)]
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
