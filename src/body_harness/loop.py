"""A mind: one seat, one body, the loop that keeps it alive.

    join -> turn -> sleep -> (the harness waits, no model) -> wake -> turn -> ...

A turn is a fresh, short conversation: the stable system prompt, then one wake message (why, the scene, the agenda,
the last few turns' notes). The model calls tools until it calls sleep. Then the harness waits on the world with the
model's condition, in pieces under the server's cap, and wakes the model only for something: the condition, a
deadline, the operator, a scheduled item, an owed line, a rules change. The seat is kept, lapses are rejoined, the
standing orders are set again after every deed, and the door is retried with backoff when the server is away."""

from __future__ import annotations

import asyncio
import difflib
import json
import re
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

import httpx

from . import caps, imagine, imports, menu, past, prompts, sandbox, scene, telemetry
from . import reflexes as rx
from .door import Door, DoorAway, DoorError, backoff
from .memory import Shelf, memo
from .providers import Model, ModelError
from .seat import Seat, append_jsonl, read_json, read_jsonl, write_json
from .skills import Skills
from .tools import CORE_NAMES, EXTRAS, Registry, compact, fix_args, parse_args, route
from .usage import Usage, append as usage_append

DEEDS = {"act", "say", "gesture", "step", "cross", "act_many"}
# a call the model got wrong, not a world that said no: these move a turn up the escalate list (a rule refusal, like
# a gangplank that will not take an elf, is the game and never escalates). The engine's layer="call" says it outright.
# The harness's own call errors by their words; the engine's by its layer (call: the shape; target: what it names)
CALL_TROUBLE = re.compile(r"bad arguments|there is no tool|\bno tool \w|Not asleep yet|\"layer\": \"(call|target)\"")
# the engine's codes for a seat this principal no longer holds (since 11-26): join again
SEAT_LOST = ("not_joined", "seat_released")


def unseated(r: Any) -> bool:
    return isinstance(r, dict) and r.get("ok") is False and r.get("code") in SEAT_LOST
READS = {"look", "find_tools", "inspect", "find", "history", "guide", "handbook", "recall", "skill", "owed", "agenda",
         "past", "menu"}
KEEP_LOADED_TURNS = 5
MAX_LOADED = 8


def _brief(args: dict[str, Any]) -> str:
    return ", ".join(f"{k}={v!r}" for k, v in args.items() if v not in (None, "", {}))[:160]


# the engine's refusals for a limited grant's reach (its under, types, scripts): another hand may cover it
_OUT_OF_REACH = re.compile(r"your grant|start with|the door refused this grant", re.I)
_TARGET_KEYS = ("entity", "parent", "thing", "target", "to", "into", "under")


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "crew"


def _world_copy_done(p: Path) -> bool:
    try:
        return bool(json.loads(p.read_text(encoding="utf-8")).get("done", True)) if p.exists() else False
    except (OSError, ValueError):
        return False


def _target_of(args: dict[str, Any]) -> str:
    return next((str(args[k]) for k in _TARGET_KEYS if isinstance(args.get(k), str) and args.get(k)), "")


def _log(seat: str, msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {seat}: {msg}", flush=True)


class Mind:
    tele = telemetry.OFF  # a seat's own sender is set in __init__; off for minds built without one

    def __init__(self, seat: Seat, client: httpx.AsyncClient):
        self.seat = seat
        self.client = client
        self.cfg = seat.config()
        self.tele = telemetry.Sender(seat.p("telemetry.jsonl"), self.cfg, {"seat": seat.name})
        self.door = Door.load(seat.p("door.json"), client)
        self.door.wait_cap = float(self.cfg.get("wait_cap", self.door.wait_cap))
        # extra grants on the same world (a builder's hand on one part of it): bh hand SEAT add NAME <invite>
        self.hands: dict[str, dict[str, Any]] = {
            f.stem: {"door": Door.load(f, client), "ops": set(), "role": ""}
            for f in (sorted(seat.p("hands").glob("*.json")) if seat.p("hands").is_dir() else [])}
        # a crew grant names several bodies (a captain and riders): the mind acts as each with as=, one sub-principal
        # per body under the same token. crew = the other bodies' names, in the seat's config
        self.crew: dict[str, dict[str, Any]] = {
            str(n).strip().lower(): {"name": str(n).strip(), "door": self.door.sub(_slug(str(n))), "joined": False}
            for n in (self.cfg.get("crew") or []) if str(n).strip()}
        self.model = Model.from_config(self.cfg.get("model", ""), {**self.cfg, "session": self.cfg.get("session") or
                                       f"bh-{seat.name}-{uuid.uuid4().hex[:6]}"})
        self.base_model = self.model
        self.rungs: dict[str, Model] = {}  # escalation models, kept so a raised max_tokens or a fallback sticks
        self.st: dict[str, Any] = seat.state()
        self.names = scene.Names(self.st.get("names"))
        self.skills = Skills(seat.home / "skills", seat.p("skills"))
        self.shelves = {"seat": Shelf(seat.p("memory")), "craft": Shelf(seat.home / "craft")}
        self.registry = Registry(self)
        self.bundle: dict[str, Any] = {}
        self.briefing: dict[str, Any] = self.st.get("briefing", {})
        self.body: dict[str, Any] = {}
        self.role = self.briefing.get("role", self.cfg.get("role", "mind"))
        self.ops: list[str] = []
        self.me = self.st.get("me", "")
        self.usage = Usage()
        self.turns = int(self.st.get("turns", 0))
        self.day_seconds = float(self.cfg.get("day_seconds", scene.DAY))
        self.stop = False
        self._linted: set[str] = set()
        self.pending: tuple[str, list[dict[str, Any]]] | None = None
        self.imagination = (imagine.Imagination(seat, self.cfg) if self.cfg.get("imagine") and imagine.available()
                            else None)

    # ---------- state ----------

    def save(self) -> None:
        self.st.update({"names": self.names.by_id, "me": self.me, "turns": self.turns, "briefing": self.briefing})
        self.seat.save_state(self.st)

    def load_tools(self, names: list[str]) -> None:
        loaded = self.st.setdefault("loaded", {})
        for n in names:
            loaded[n] = self.turns

    def loaded_tools(self) -> list[str]:
        loaded = self.st.setdefault("loaded", {})
        keep = {n: t for n, t in loaded.items() if self.turns - t <= KEEP_LOADED_TURNS}
        keep = dict(sorted(keep.items(), key=lambda kv: -kv[1])[:MAX_LOADED])
        self.st["loaded"] = keep
        return list(keep)

    def apps(self) -> dict[str, dict[str, Any]]:
        return sandbox.app_specs(self.seat.p("apps"))

    def verb_names(self) -> set[str]:
        b = self.bundle
        out = {str(a.get("verb")) for a in (b.get("affordances") or []) if isinstance(a, dict) and a.get("verb")}
        out |= {str(v).split(":")[0].strip() for v in (b.get("verbs") or [])}
        return out - {""}

    def present_names(self) -> list[str]:
        b = self.bundle
        names: list[Any] = []
        # a full perceive has present/affordances; a brief one (newer engines) people/things, exits as plain names
        for key in ("present", "people", "things", "holding", "exits"):
            names += [x.get("name") if isinstance(x, dict) else scene.bare(x) for x in (b.get(key) or [])]
        here = b.get("here")  # the place the body is in: a cart it rides is a target too (halt, alight)
        names.append(here.get("name") if isinstance(here, dict) else here)
        return [n for n in names if isinstance(n, str) and n]

    def verb_targets(self, verb: Any) -> list[str]:
        """The names the perceive lists `verb` against: what breaks a tie between two names that both match."""
        if not isinstance(verb, str) or not verb:
            return []
        out = [a.get("target_name") or self.names.name(a.get("target"))
               for a in (self.bundle.get("affordances") or []) if isinstance(a, dict) and a.get("verb") == verb]
        return [n for n in out if isinstance(n, str) and n]

    def verb_params(self, verb: Any) -> set[str] | None:
        """The params the perceive declares for `verb`, or None when it lists the verb without saying (or not at all)."""
        listed = [a for a in ((getattr(self, "bundle", None) or {}).get("affordances") or [])
                  if isinstance(a, dict) and a.get("verb") == verb]
        if not listed or any(not isinstance(a.get("params"), dict) for a in listed):
            return None
        return {k for a in listed for k in a["params"]}

    # ---------- the world ----------

    async def join(self) -> dict[str, Any]:
        entity = self.cfg.get("entity")
        j = await self.door.op("join", entity=entity) if entity else await self.door.op("join")
        if isinstance(j, dict) and j.get("ok") is False:
            raise DoorError(f"join refused: {j.get('reason')} ({j.get('hint', '')})")
        if j.get("briefing"):
            self.briefing = j["briefing"]
            self.role = self.briefing.get("role", self.role)
        self.me = (j.get("you") or {}).get("id", self.me)
        self._keep_bundle(j)
        await self._read_body()
        return j

    async def _read_body(self) -> None:
        """The body's own description, kind and class, once per join, for the system prompt (inspect leaves no trace).
        A world that refuses it leaves the prompt as it was."""
        if not self.me:
            return
        try:
            r = await self.door.op("inspect", entity=self.me)
        except Exception:
            return
        st = (r.get("state") if isinstance(r, dict) else None) or {}
        self.body = {k: st[k] for k in ("description", "people", "cls") if isinstance(st.get(k), str) and st[k]}

    def _keep_bundle(self, b: dict[str, Any]) -> None:
        if not isinstance(b, dict) or "you" not in b:
            return
        self.bundle = {k: v for k, v in b.items() if k != "briefing"}
        self.names.learn(b)
        text = json.dumps(self.bundle, default=str)
        self.seat.p("cache", "perceive.json").write_text(text, encoding="utf-8")
        self.seat.p("workspace", "scene.json").write_text(text, encoding="utf-8")
        if b.get("rules_version") is not None:
            self.st["rules_version"] = b["rules_version"]

    async def perceive(self) -> dict[str, Any]:
        b = await self.door.op("perceive", events=40)
        if unseated(b):
            await self.join()
            b = await self.door.op("perceive", events=40)
        self._keep_bundle(b)
        return b

    async def ride_out(self, why: Exception | str) -> float:
        """Wait out a server that is away, in this process: probe with backoff and jitter (2 s doubling to 5
        minutes, so thirty seats do not knock in step), then join again, since a restarted server may not hold the
        seat. Returns the seconds it was away. No supervisor is needed for a restart or a redeploy; only a refused
        grant (401/403) ends the seat."""
        t0, attempt, said = time.time(), 0, False
        _log(self.seat.name, f"server away ({why}); riding it out")
        self.tele.emit("seat", {"event": "server away"})
        while not self.stop:
            await asyncio.sleep(backoff(attempt, 2.0, float(self.cfg.get("away_cap", 300))))
            attempt += 1
            away = time.time() - t0
            if away > 600 and not said:
                said = True
                self.seat.outbox(f"the server has been away {away / 60:.0f} min; still trying, nothing to do", "note")
            if not await self.door.alive():
                continue
            try:
                await self.join()
            except DoorError as e:
                if e.status in (401, 403):
                    raise
                _log(self.seat.name, f"server answers but join failed ({e}); trying again")
                continue
            away = time.time() - t0
            _log(self.seat.name, f"server back after {away:.0f}s; joined again")
            if said:
                self.seat.outbox(f"the server is back after {away / 60:.0f} min", "note")
            return away
        return time.time() - t0

    @staticmethod
    def _away_reason(secs: float) -> str:
        span = f"{secs / 60:.0f} minutes" if secs >= 90 else f"{secs:.0f} seconds"
        return f"the server was away for {span} and you are back in your seat; look before you act"

    async def start(self) -> None:
        await self.join()
        self.tele.labels.update(world=str(self.briefing.get("world") or ""), character=self.my_name(), role=self.role)
        self.tele.start(self.client)
        self.tele.emit("seat", {"event": "restart" if self.turns else "start", "turns": self.turns,
                                "model": self.model.spec})
        self.tele.emit("setup", {"setup": telemetry.safe_setup(self.cfg)})
        try:
            o = await self.door.get("/op")
            self.ops = o.get("ops", [])
            self.role = o.get("role", self.role)
        except DoorError:
            pass
        for h, hand in list(self.hands.items()):
            try:
                o = await hand["door"].get("/op")
                hand["ops"], hand["role"] = set(o.get("ops") or []) - set(self.ops), o.get("role", "")
                _log(self.seat.name, f"hand {h}: {hand['role']}, {len(hand['ops'])} ops the mind's grant lacks")
            except DoorError as e:
                _log(self.seat.name, f"hand {h}: not used this run, its door refused ({e})")
                del self.hands[h]
        await self.registry.load_world_ops()
        try:
            hb = await self.door.op("handbook")
            self.st["handbook"] = [{"path": "SKILL.md", "description": "the handbook's own index"}] + \
                                  [p for p in (hb.get("pages") or []) if isinstance(p, dict)]
        except DoorError:
            pass
        if self.cfg.get("past") and self.me and not _world_copy_done(self.seat.p(past.WORLD_FILE)):
            # a body that lived before this harness ran it keeps its deeds searchable: the world's log is the source.
            # In the background: on a busy world it takes minutes, and the seat plays meanwhile
            self._past_task = asyncio.create_task(self._import_past())
        self.save()

    async def _import_past(self) -> None:
        try:
            _log(self.seat.name, "past: " + await past.import_world(self.door, self.me, self.names.by_id,
                                                                    self.seat.dir))
        except (DoorError, OSError, ValueError) as e:
            _log(self.seat.name, f"past: the world's history stopped ({e}); what was read is kept, and the next "
                                 f"start or bh past-import goes on from there")

    # ---------- tools ----------

    def _hands_for(self, name: str, args: dict[str, Any]) -> list[str]:
        """The hands to try for an op the mind's grant lacks: the one that last did a deed on this target first."""
        hs = [h for h, hand in self.hands.items() if name in hand["ops"]]
        last = (self.st.get("hand_for") or {}).get(_target_of(args))
        return sorted(hs, key=lambda h: h != last)

    async def _op(self, name: str, args: dict[str, Any]) -> Any:
        """One world op, through the mind's door, or for an author op through the hand that covers the target. A
        hand whose reach the target is outside refuses, and the next hand is tried; the refusal names each one."""
        args = dict(args)
        who = str(args.pop("as", "") or "").strip()
        if who and who.lower() not in (self.my_name().lower(), self.me.lower()):
            return await self._crew_op(who, name, args)
        hands = self._hands_for(name, args)
        if not hands:
            return await self.door.op(name, **args)
        tried: list[str] = []
        r: Any = None
        for h in hands:
            try:
                r = await self.hands[h]["door"].op(name, **args)
            except DoorError as e:
                r = {"ok": False, "reason": str(e)}
            if not (isinstance(r, dict) and r.get("ok") is False):
                if (t := _target_of(args)):
                    self.st.setdefault("hand_for", {})[t] = h
                    self.st["hand_for"] = dict(list(self.st["hand_for"].items())[-200:])
                return r
            tried.append(f"{h}: {r.get('reason')}")
            if not _OUT_OF_REACH.search(str(r.get("reason") or "")):
                break  # refused for what the op asks, not for where: every hand would say the same
        r = dict(r)
        r["reason"] = f"{r.get('reason')} (through your hand {', '.join(t.split(':')[0] for t in tried)})"
        if len(tried) > 1:
            r["hands_tried"] = tried
        return r

    async def _crew_op(self, who: str, name: str, args: dict[str, Any]) -> Any:
        """One op as another body of the crew: its own seat under this grant, taken on first use."""
        low = who.lower()
        c = self.crew.get(low) or next((c for k, c in self.crew.items() if low in k or k in low), None)
        if c is None:
            names = ", ".join(c["name"] for c in self.crew.values())
            return {"ok": False, "reason": f"as={who!r} is not one of your crew",
                    "hint": f"as= one of: {names}, or leave it out to act as yourself" if names else
                    "this seat has no crew: leave as= out"}
        for attempt in (1, 2):
            try:
                if not c["joined"]:
                    j = await c["door"].op("join", entity=c["name"])
                    if isinstance(j, dict) and j.get("ok") is False:
                        return {"ok": False, "reason": f"could not take {c['name']}'s seat: {j.get('reason')}",
                                "hint": j.get("hint") or "the world's owner names a crew's bodies in its grant"}
                    c["joined"] = True
                r = await c["door"].op(name, **args)
            except DoorError as e:
                return {"ok": False, "reason": f"as {c['name']}: {e}"}
            lost = unseated(r)
            if not lost or attempt == 2:
                return r
            c["joined"] = False  # the world restarted and forgot the seat: take it again once
        return r

    async def call(self, name: str, args: dict[str, Any], turn: dict[str, Any]) -> str:
        if name == "look":
            if args.get("part"):
                if args["part"].strip().lower() in ("affordances", "you can", "verbs"):
                    return "\n".join(scene.affordance_lines(self.bundle.get("affordances") or [], True)
                                     or [str(v) for v in (self.bundle.get("verbs") or [])])
                return compact(scene.part(self.bundle, args["part"]), 8000)
            b = await self.perceive()
            if self.cfg.get("menu"):  # a fresh look is a fresh menu: its numbers replace the wake's
                evs = b.get("events") or []
                self.menu = menu.Menu(b, self.names, self.me, self.day_seconds, rank=bool(self.cfg.get("menu_rank")),
                                      lists=await self._log_windows(evs) if self.cfg.get("menu_lists") else None)
                return self.menu.render()
            return scene.card(b, self.names, self.me, self.day_seconds)
        if name == "menu" and getattr(self, "menu", None) is not None:
            turn["opens"] = turn.get("opens", 0) + 1
            at = str(args.get("open") or args.get("more") or "")
            if args.get("open") and not args.get("more") and "." not in at and "+" not in at:
                at += ".1"  # open="3": its first item
            got = self.menu.resolve(at.rstrip("+"), str(args.get("by") or ""), str(args.get("find") or ""))
            return await self._menu_run(got)
        if name == "sleep":
            # on the turn's last step no call is left to put a refusal right: the model's own sleep stands (its note,
            # plan and watch), and what arrived meanwhile is the next wake's news. Refused there, 29 of a pilot's
            # turns ended on "(the turn ran out of steps)" and lost their note (Niiro Cradle, 25 Nov)
            return await self._sleep(args, turn, forced=bool(turn.get("last_step")))
        if name == "think" and self.cfg.get("think_tool"):
            turn["thoughts"] = turn.get("thoughts", 0) + 1
            return "(noted)"
        if name == "find_tools":
            return compact(self.registry.find(str(args.get("query", ""))))
        if name == "records" and imports.entries(self.seat.dir):
            return compact(await asyncio.to_thread(imports.records, self.seat.dir, args), 8000)
        if name == "past" and self.cfg.get("past"):
            return compact(await asyncio.to_thread(past.past, self.seat.p("calls.jsonl"), args), 6000)
        if name in ("imagine", "reflect", "relive") and self.imagination is not None:
            if name == "reflect":
                return compact(self.imagination.reflect(args), 6000)
            if name == "relive":
                return compact(await asyncio.to_thread(self.imagination.relive, args, self.my_name()), 6000)
            turn["imagined"] = turn.get("imagined", 0) + 1
            if turn["deeds"] > turn.get("deeds_seen", 0):
                # the card was read before this turn's deeds: Tomo imagined from Oren at 11 hp when he was at 6, and
                # Suri from a cooldown of 3 that was 1, then each was told it had been surprised
                await self.perceive()
                turn["deeds_seen"] = turn["deeds"]
            # a few hundred small worlds: off the event loop, so the other seats in this process keep waiting
            return compact(await asyncio.to_thread(self.imagination.imagine, args, self.bundle, self.my_name(),
                                                   self.turns, self.verb_names()), 6000)
        if name == "tell":
            return compact(await self._tell(args))
        if name == "memo":
            return compact(memo(self.shelves, **{k: v for k, v in args.items()
                                                 if k in ("op", "name", "text", "hook", "shelf", "query")}), 8000)
        if name == "skill":
            return self.skills.read(str(args.get("name", "")), str(args.get("part", "") or ""))[:12000]
        if name in EXTRAS:
            return compact(await self._extra(name, args), 8000)
        if name.startswith("app_"):
            spec = self.apps().get(name)
            if not spec:
                return compact({"ok": False, "reason": f"no app {name}"})
            self.load_tools([name])
            return compact(await sandbox.run_app(self.seat.home, self.seat.dir, spec["file"], args), 6000)
        # a world op
        if name not in ("act", "say", "remember", "intend") and name not in self.registry.world:
            if name in self.verb_names():
                # read(target=...) for act(verb='read', target=...): a verb called as if it were a tool
                rest = dict(args)
                target = rest.pop("target", None) or rest.pop("entity", None)
                params = rest.pop("params", None) or {}
                out = await self.call("act", {"verb": name, "target": target, "params": {**params, **rest}}, turn)
                return f"(harness: {name} is a verb: read as act(verb='{name}', ...))\n" + out
            meant = route(name, args)
            append_jsonl(self.seat.p("invented.jsonl"), {"ts": time.time(), "model": self.model.spec, "tool": name,
                                                         "args": sorted(args), "read_as": meant[0] if meant else None})
            self.tele.emit("invented", {"model": self.model.spec, "tool": name, "read_as": meant[0] if meant else None})
            if meant and (meant[0] != "act" or meant[1]["verb"] in self.verb_names() or not self.verb_names()):
                out = await self.call(meant[0], meant[1], turn)
                return f"(harness: there is no tool {name}; read as {meant[0]}({_brief(meant[1])}))\n" + out
            known = [t["function"]["name"] for t in self.registry.tools(self.loaded_tools())] + sorted(self.verb_names())
            near = difflib.get_close_matches(name, known, n=3, cutoff=0.6)
            return compact({"ok": False, "reason": f"no tool {name}",
                            "hint": (f"did you mean {', '.join(near)}? " if near else "") +
                                    "find_tools(query) finds and loads tools; verbs go through act(verb, ...)"})
        fixed, notes = fix_args(name, args, self)
        if name == "act" and self.cfg.get("strict_params"):
            # Klhyts Refuge, 28 Nov: Corra staked one copper as drop(Coin, params={'n': 1}); drop takes count, the
            # engine read none and her whole purse of 57 hit the table. A param the verb does not declare is sent
            # back with what it does declare, so the model names it itself. A setting for models that misname
            # params: Gemma 4 had 27 of 115 acts sent back for params the engine would have ignored
            takes = self.verb_params(fixed.get("verb"))
            extra = undeclared(fixed.get("params"), takes)
            if extra:
                return compact({"ok": False, "reason": f"{fixed.get('verb')} takes no {', '.join(extra)}; "
                                                       "nothing was done",
                                "takes": sorted(takes) or "no params"})
        if name == "act" and self.imagination is not None and self.imagination.spec["bound"] and                 imagine._key(str(fixed.get("verb") or "")) not in self.imagination.bound_plans()[0]:
            # a deed outside the plan ends its run: score it now, before (say) a rest heals what the plan cost.
            # Tamsin sparred three times and rested in the same turn; read at the next wake, she was whole again
            done = await self._score_imagined()
            if done:
                notes.append(done.strip())
        if name == "say":
            text = str(fixed.get("text", ""))
            # the same news to someone else is a report, not a repeat; a world hour is the window
            to, now, window = str(fixed.get("to") or ""), time.time(), float(self.cfg.get("repeat_window", 900))
            prev = _repeats(text, [x[2] for x in self.st.get("said", []) if len(x) == 3 and x[1] == to and
                                   now - x[0] < window], self.names.by_id.values())
            if prev and turn.get("repeat_ok") != text:
                turn["repeat_ok"] = text  # the same line again goes through: the model meant it
                return compact({"ok": False, "reason": f"not said: you said nearly this a moment ago: {prev[:160]!r}",
                                "hint": "answer what was said to you, add only what is new, or say nothing; the "
                                        "same line again says it anyway"})
            # two seats that wake on each other's words traded questions for minutes with nothing done (Corra and
            # Ssareth): past a hard count the line is held once, as AI Town ends a conversation after 8 messages
            long = {p: n for p, n in (self.st.get("chat") or {}).items() if n >= int(self.cfg.get("chat_hard", 8))}
            who = next((p for p in long if to and (to.lower() in p.lower() or p.lower() in to.lower())), None) or \
                (next(iter(long)) if long and not to else None)
            if who and not turn.get("chat_ok"):
                turn["chat_ok"] = True
                return compact({"ok": False, "reason": f"not said: you and {who} have only talked for {long[who]} "
                                                       f"turns, with nothing done between",
                                "hint": "do something (act), or let it rest and sleep on a change (someone arriving, "
                                        "a deed, the hour). If this line truly moves things, say it again"})
        if name == "away":
            self.st["away"] = fixed
        sent = fixed
        fixed = self._drop_unsupported(name, fixed)
        r = await self._op(name, fixed)
        bad = re.search(r"unexpected keyword argument '(\w+)'", str(r)) if isinstance(r, dict) else None
        if bad and bad.group(1) in fixed:
            # an older engine has no such parameter (Klhyts Refuge's say takes no gesture): drop it for this seat
            # from now on and send the call again, rather than spend the model's step on a refusal
            self.st.setdefault("unsupported", {}).setdefault(name, []).append(bad.group(1))
            fixed = self._drop_unsupported(name, fixed)
            r = await self._op(name, fixed)
        if (dropped := sorted(set(sent) - set(fixed))):
            notes.append(f"this world's {name} takes no {', '.join(dropped)}: sent without it" +
                         (" (the gesture went into the line as *...*)" if "gesture" in dropped and name == "say"
                          else ""))
        if unseated(r):
            await self.join()
            r = await self._op(name, fixed)
        ok = not (isinstance(r, dict) and r.get("ok") is False)
        if ok and name in DEEDS:
            turn["deeds"] += 1
            if name != "say" and not (name == "act" and fixed.get("verb") == "talk"):
                turn["done"] = turn.get("done", 0) + 1  # a deed that moves the world, not more words
            if name == "act":
                turn.setdefault("verbs", set()).add(str(fixed.get("verb") or ""))
                if self.imagination is not None and self.imagination.bind(str(fixed.get("verb") or ""),
                                                                          fixed.get("target")):
                    notes.append("this is part of a plan you imagined: when the plan is done, what came of it is "
                                 "compared with what you imagined")
        if ok and name in ("say", "act") and (fixed.get("to") or (name == "act" and fixed.get("verb") == "talk")):
            who = fixed.get("to") or fixed.get("target")
            if isinstance(who, str) and who:
                turn.setdefault("partners", []).append(who)
        if ok and name == "say":
            self.st["said"] = (self.st.get("said", []) + [[time.time(), str(fixed.get("to") or ""),
                                                           str(fixed.get("text", ""))]])[-8:]
            n = self._count_unanswered(str(fixed.get("to") or ""), turn)
            if n >= int(self.cfg.get("unanswered", 3)):
                # the log line is a record of the note this say got back, after the mind's Nth turn of lines
                turn.setdefault("nudges", []).append(f"unanswered: told after turn {n} to {fixed['to']}")
                # no "mind" here: seats copied "X has no mind" into notes, and from notes it can reach speech
                notes.append(f"{fixed['to']} has not answered you over your last {n} turns of speaking to them. "
                             f"Some people keep to themselves and may not answer: ask someone else, look for another "
                             f"way, or wait until they speak to you")
        if name in self.registry.world:
            self.load_tools([name])
        if isinstance(r, dict) and isinstance(r.get("more"), dict) and r["more"].get("handle") \
                and "more" in self.registry.world:
            # a cut answer says to call more(handle): the tool comes with it, or the next call is a "no tool" refusal
            self.load_tools(["more"])
        if isinstance(r, dict) and isinstance(r.get("events"), list):
            self._keep_heard(r["events"])
            lines = scene.event_lines(r["events"], self.me, self.names, 12)
            r = {k: v for k, v in r.items() if k not in ("events", "cursor")}
            if lines:
                r["what happened"] = lines
        if name == "timers":
            r = _real_timers(r, self._rate())
        if ok and name == "say" and not fixed.get("to"):
            gone = self.names.absent_named(str(fixed.get("text", "")), self.present_names(), self.me)
            if gone:
                notes.append(f"{', '.join(gone)} {'is' if len(gone) == 1 else 'are'} not here; only who is here "
                             f"heard that. To reach them, go where they are")
        out = compact(r)
        if notes:
            out = f"(harness: {'; '.join(notes)})\n" + out
        return out

    async def _log_windows(self, evs: list[dict[str, Any]]) -> dict[str, Any]:
        """For menu_lists: the talk with whoever spoke to this seat and this body's own deeds, read from the world's
        log (a mind reads only its own body's). A window that came back full says the log goes further back."""
        me_name = self.my_name()
        speaker = next((e.get("actor") for e in evs if scene.to_me(e, self.me, me_name, self.names) and e.get("actor")), None)

        async def read(op: str, args: dict[str, Any]) -> Any:
            try:
                r = await self._op(op, args)
            except Exception:
                return None
            return None if isinstance(r, dict) and r.get("ok") is False else r

        # an engine with history's between/actor/seq and history_count filters in the world; an older one is read
        # in a window and filtered here, and its cuts can only say that the log goes further back
        if getattr(self, "_history_v2", None) is None:  # asked once a run: the engine may be upgraded between runs
            self._history_v2 = await read("history_count", {"actor": self.me}) is not None
        v2 = bool(self._history_v2)
        out: dict[str, Any] = {"v2": v2}
        talk_kinds = ["say", "talked"]
        if speaker:
            if v2:
                talk = await read("history", {"between": speaker, "kinds": talk_kinds, "limit": 30, "newest": True})
                n = await read("history_count", {"between": speaker, "kinds": talk_kinds})
                talk = [e for e in talk or [] if isinstance(e, dict)]
                out.update({"with": speaker, "talk": talk, "talk_total": int((n or {}).get("total") or len(talk)),
                            "talk_full": len(talk) >= 30})
            else:
                talk = [e for e in await read("history", {"kinds": talk_kinds, "limit": 80, "newest": True}) or []
                        if isinstance(e, dict)]
                out.update({"with": speaker, "talk_full": len(talk) >= 80,
                            "talk": menu.Menu.pick(talk, {"with": speaker, "keep": 30})})
        raw = await read("history", {"actor": self.me, "limit": 60, "newest": True} if v2 else
                         {"limit": 120, "newest": True}) or []
        raw = [e for e in raw if isinstance(e, dict)]
        out.update({"deeds_full": len(raw) >= (60 if v2 else 120),
                    "deeds": menu.Menu.pick(raw, {"deeds_of": self.me, "keep": 30})})
        return out

    async def _menu_run(self, got: tuple[Any, ...]) -> str:
        """What a menu item opens: text the harness holds, or one read op. Read straight through the door, so an
        open loads no tool and the seat's tool list stays what it would be on the scene card."""
        if got[0] == "text":
            return str(got[1])[:8000]
        if got[0] == "error":
            return compact({"ok": False, "reason": got[1], "hint": got[2]})
        r = await self._op(got[1], dict(got[2]))
        if len(got) > 3 and got[3].get("full"):  # one event, in full: who, what, where, what caused it
            return compact(r[0], 3000) if isinstance(r, list) and r else "no such event in your log"
        if len(got) > 3 and isinstance(r, list):
            # an absence is only as wide as the window read: shown 8 of its last deeds, a seat denied the walk to the
            # Chapel it had made a day before (own-walk, 16/30 to 9/30), so every cut says it is a cut
            full = len(r) >= int(got[2].get("limit") or 0) > 0
            mine = menu.Menu.pick([e for e in r if isinstance(e, dict)], {**got[3], "keep": 10 ** 6})
            r = mine[-int(got[3].get("keep") or 20):]
            if not r:
                return ("none in the newest part of the world's log; it goes further back" if full else
                        "none: no deed of yours is on record in the world's log" if got[3].get("deeds_of")
                        else "none further back")
            head = (f"(the newest {len(r)} of {len(mine)}, since t={float(r[0].get('t') or 0):.0f}; older ones are "
                    "further back in the log)" if len(mine) > len(r) or full else "")
            return "\n".join(([head] if head else []) + scene.event_lines(r, self.me, self.names, len(r)))
        if isinstance(r, list) and r and all(isinstance(e, dict) and "kind" in e for e in r):
            return "\n".join(scene.event_lines(r, self.me, self.names, 20)) or "(nothing further back)"
        if isinstance(r, dict) and isinstance(r.get("events"), list):
            r = {**{k: v for k, v in r.items() if k not in ("events", "cursor")},
                 "events": scene.event_lines(r["events"], self.me, self.names, 20)}
        return compact(r, 6000)

    async def _tell(self, a: dict[str, Any]) -> Any:
        what, why = str(a.get("what", "")).strip(), str(a.get("why", "") or "").strip()
        if a.get("to") == "operator":
            self.tele.emit("tell", {"to": "operator", "about": str(a.get("kind") or "note")[:24],
                                    "out_chars": len(what) + len(why)})
            self.seat.outbox(what + (f"\n{why}" if why else ""), a.get("kind") or "note")
            _log(self.seat.name, f"to operator: {what[:200]}")
            return {"ok": True, "sent": "to your operator"}
        kind = a.get("kind") or "other"
        if kind in ("flaw", "vulnerability"):
            self.tele.emit("tell", {"to": "owner", "about": kind, "out_chars": len(what) + len(why)})
            return await self.door.op("report_flaw", what=what, how=why, severity=a.get("severity") or "medium")
        kind = {"feature": "capability"}.get(kind, kind)
        self.tele.emit("tell", {"to": "owner", "about": str(kind)[:24], "out_chars": len(what) + len(why)})
        r = await self.door.op("ask_owner", kind=kind, what=what[:300], why=why[:1000], credit=a.get("credit"))
        return r

    async def _extra(self, name: str, a: dict[str, Any]) -> Any:
        if name == "run_python":
            return await sandbox.run_python(self.seat.home, self.seat.dir, str(a.get("code", "")),
                                            float(a.get("timeout") or 30))
        if name == "write_file":
            return sandbox.write_file(self.seat.dir, str(a.get("path", "")), str(a.get("text", "")))
        if name == "schedule":
            op = a.get("op") or "add"
            items = read_jsonl(self.seat.p("schedule.jsonl"))
            if op == "list":
                return [i for i in items if not i.get("done")]
            if op == "drop":
                for i in items:
                    if i.get("id") == a.get("id"):
                        i["done"] = True
                self.seat.p("schedule.jsonl").write_text("".join(json.dumps(i) + "\n" for i in items),
                                                         encoding="utf-8")
                return {"ok": True}
            if not a.get("what") or (a.get("at_t") is None and a.get("in_minutes") is None):
                return {"ok": False, "reason": "give what= and at_t= (world seconds) or in_minutes= (real)"}
            item = {"id": uuid.uuid4().hex[:6], "what": a["what"],
                    "at_t": a.get("at_t"), "at_ts": time.time() + float(a["in_minutes"]) * 60
                    if a.get("in_minutes") is not None else None}
            append_jsonl(self.seat.p("schedule.jsonl"), item)
            return {"ok": True, "scheduled": item}
        if name == "agenda":
            return self.agenda()
        return {"ok": False, "reason": f"no extra {name}"}

    # ---------- sleep and wake ----------

    def _lint(self, until: str) -> str | None:
        """Catch the watch traps minds fell into, once per condition: heard('<a name>') waits for the word, not
        the person; heard('<your own name>') wakes on your own lines."""
        if until in self._linted:
            return None
        self._linted.add(until)
        me = str((self.bundle.get("you") or {}).get("name") or "").lower()
        known = [n.lower() for n in self.names.by_id.values()]
        for m in re.finditer(r"heard\(\s*['\"]([^'\"]+)['\"]\s*\)", until):
            w = m.group(1).strip().lower()
            if me and (w == me or w in me.split()):
                return (f"heard('{m.group(1)}') wakes on your own lines too (your name is in them). to_me() already "
                        f"wakes you when someone addresses you. Sleep again with a fixed condition, or the same one "
                        f"if you meant it.")
            if any(w == n or w in n.split() for n in known):
                return (f"heard('{m.group(1)}') wakes on any line with the word '{m.group(1)}' in it, not on what "
                        f"{m.group(1)} says. To wake when they speak: heard('', by='{m.group(1)}'). Sleep again "
                        f"with a fixed condition, or the same one if you meant the word.")
        return None

    async def _score_imagined(self) -> str:
        """Read what the bound plans came to, with inspect (a read that leaves no trace, so "since I last looked"
        stays where it was), and score what the seat imagined. Held things are not read here: only state numbers."""
        im = self.imagination
        if im is None or not im.spec["bound"]:
            return ""
        me = self.my_name()
        card: dict[str, Any] = {"you": {}, "present": []}
        for name in {me} | im.bound_plans()[1]:
            eid = self.me if name == me else next((i for i, n in self.names.by_id.items() if n == name), name)
            try:
                r = await self.door.op("inspect", entity=eid)
            except Exception:
                continue
            if not isinstance(r, dict) or r.get("ok") is False:
                continue
            if name == me:
                card["you"] = r
            else:
                card["present"].append({**r, "name": name})
        return im.wake_text(im.score(card, me))

    async def _sleep(self, a: dict[str, Any], turn: dict[str, Any], forced: bool = False) -> str:
        if self.imagination is not None and self.imagination.spec["bound"]:
            # the turn ends the plan's run: score it now, and the seat reads it when it wakes
            self.st["imagined_text"] = self.st.get("imagined_text", "") + await self._score_imagined()
        until = (a.get("until") or "").strip() or self.cfg.get("default_until", "to_me()")
        minutes = float(a.get("minutes") or self.cfg.get("default_minutes", 60))
        # min_minutes=0 is for test worlds where nothing moves unless the seat does: Rin in the Dojo sat out a full
        # minute after every turn, two thirds of her run
        minutes = max(float(self.cfg.get("min_minutes", 1)), min(minutes, float(self.cfg.get("max_minutes", 480))))
        if not forced:
            if turn.get("spoken_to") and not turn["deeds"] and not turn.get("silence_checked"):
                # a mind's note said "told the Traveler" over a turn with no say in it: words in a note are not deeds
                turn["silence_checked"] = True
                return ("Not asleep yet: you woke because someone spoke to you, and this turn you have not said or "
                        "done anything; nobody heard an answer. If your character answers, say it now. If silence "
                        "is the answer, sleep again.")
            warn = self._lint(until)
            if warn:
                return "Not asleep yet: " + warn
        rules: list[dict[str, Any]] = []
        waiting_for = str(a.get("waiting_for") or "").strip()
        if self.cfg.get("reflexes") and not forced and a.get("reflexes"):
            rules, bad = rx.check(a.get("reflexes"), waiting_for, self.cfg)
            if bad:
                return "Not asleep yet: " + "; ".join(bad)
        if "said_here()" in until:
            # the harness's shorthand: anyone but you said something here (heard('') would hear your own lines too)
            mine = json.dumps(str((self.bundle.get("you") or {}).get("name") or ""))
            until = until.replace("said_here()", f"any(e['kind'] in ('say', 'talked') and e['actor'] != {mine} "
                                                  f"for e in events)")
        partners = [p for p in dict.fromkeys(turn.get("partners") or []) if p.lower() not in until.lower()][:3]
        if partners:
            # someone you are talking with may answer the room, or call you by a nickname: to_me() misses both
            until = f"({until}) or " + " or ".join(f"heard('', by={json.dumps(p)})" for p in partners)
        if self.cfg.get("always_to_me", True) and "to_me()" not in until.replace(" ", ""):
            # whatever else it waits for, a character always hears words aimed at it
            until = f"to_me() or ({until})"
        if turn["deeds"] and self.st.get("away"):
            # a deed ends the standing orders; set them again before sleeping
            await self.door.op("away", **self.st["away"])
        # arm it once, briefly: the engine checks the grammar, and anything that happened while we decided comes now
        w = await self.door.wait(until, 1)
        if isinstance(w, dict) and w.get("ok") is False:
            if forced:
                until = "to_me()"
            else:
                return compact({"refused": w.get("reason"), "hint": w.get("hint"),
                                "fix": "write the condition again (skill('waiting') has the grammar), then sleep"})
        elif isinstance(w, dict) and w.get("fired"):
            # what came while it decided is heard now: a reply here was shown to the model but never cleared its
            # speaker from the unanswered list, so the next line to them said they had not answered (Taan and Sefa,
            # Ruhl and Tobin, Marr and Ossa: 7 pairs in 45 minutes, Glass Reach, 26 Nov)
            self._keep_heard(w.get("events") or [])
            if forced and w.get("already"):
                # the old watch is true now (a time that has passed): keeping it would wake the seat at once, every
                # time; listen for words until the model sets a new one
                until = "to_me()"
            elif forced:
                # the turn is over either way; what fired is the next wake's news, not lost
                self.pending = ("it happened while you were deciding", w.get("events") or [])
            elif w.get("already"):
                return ("Not asleep: that condition is already true now, so it would wake you at once. Pick one "
                        "that is false now (a change you are waiting for), then sleep.")
            else:
                lines = scene.event_lines(w.get("events") or [], self.me, self.names)
                return ("Not asleep: while you were deciding, it happened:\n" + "\n".join(lines) +
                        "\nAnswer it if it needs you, then sleep again.")
        self.st["watch"] = {"until": until, "deadline": time.time() + minutes * 60, "armed": time.time()}
        # every sleep sets its rules afresh: none given, none kept
        self.st["reflexes"] = {"waiting_for": waiting_for, "rules": rules, "turn": self.turns} if rules else None
        note = str(a.get("note") or "").strip()
        turn["note"] = note
        turn["slept"] = True
        if str(a.get("plan") or "").strip():
            # the plan is replaced, never appended: an old belief cannot ride forward beside the new one
            self.st["plan"] = str(a["plan"]).strip()[:500]
        return f"Asleep until {until!r} or {minutes:.0f} minutes."

    def _due(self, t_world: float | None) -> list[dict[str, Any]]:
        now = time.time()
        due = []
        for i in read_jsonl(self.seat.p("schedule.jsonl")):
            if i.get("done"):
                continue
            if (i.get("at_ts") and i["at_ts"] <= now) or (i.get("at_t") is not None and t_world is not None
                                                           and float(i["at_t"]) <= t_world):
                due.append(i)
        return due

    def _mark_done(self, ids: set[str]) -> None:
        items = read_jsonl(self.seat.p("schedule.jsonl"))
        for i in items:
            if i.get("id") in ids:
                i["done"] = True
        self.seat.p("schedule.jsonl").write_text("".join(json.dumps(i) + "\n" for i in items), encoding="utf-8")

    def _next_real_due(self) -> float | None:
        ts = [i["at_ts"] for i in read_jsonl(self.seat.p("schedule.jsonl")) if not i.get("done") and i.get("at_ts")]
        return min(ts) if ts else None

    async def wait_for_wake(self) -> tuple[str, list[dict[str, Any]]]:
        """Wait on the world with no model in the loop. Returns (why, events)."""
        if self.pending:
            p, self.pending = self.pending, None
            return p
        watch = self.st.get("watch") or {"until": "to_me()", "deadline": time.time() + 3600}
        until = watch["until"]
        last_look = time.time()
        inbox_seen = float(self.st.get("inbox_seen", time.time()))
        present = self._player_here()
        while not self.stop:
            now = time.time()
            if self.seat.inbox_new(inbox_seen):
                return "your operator wrote to you (see the agenda)", []
            due_real = self._next_real_due()
            if due_real and due_real <= now:
                due = self._due(None)
                self._mark_done({i["id"] for i in due})
                return "something you scheduled is due: " + "; ".join(i["what"] for i in due), []
            if now >= watch["deadline"]:
                if await self._reflex([], "timeout"):
                    watch = self.st.get("watch") or watch
                    continue
                return f"time is up on your watch ({until!r} did not happen)", []
            # short pieces so the operator's word and real-time schedules land within half a minute; one light
            # request per piece per seat. (A wait is never cancelled mid-flight: the server would move the cursor
            # past whatever fired.)
            piece = float(self.cfg.get("wait_piece", 30))
            step = min(self.door.wait_cap, piece, watch["deadline"] - now, (due_real - now) if due_real else 1e9)
            try:
                w = await self.door.wait(until, max(5.0, step), present=present or None)
            except DoorError as e:
                if e.status in (401, 403):
                    raise
                return self._away_reason(await self.ride_out(e)), []
            if isinstance(w, dict) and w.get("ok") is False:
                reason = str(w.get("reason", ""))
                if unseated(w):
                    _log(self.seat.name, f"seat lapsed ({reason[:80]}); joining again")
                    await self.join()
                    continue
                return f"your watch was refused: {reason} ({w.get('hint', '')}); set a new one", []
            if w.get("fired"):
                if await self._reflex(w.get("events") or [], "fired"):
                    continue  # the body answered by the mind's own rule; the watch goes on from the new cursor
                return f"what you waited for happened ({until})", w.get("events") or []
            due = self._due(float(w.get("t") or 0))
            if due:
                self._mark_done({i["id"] for i in due})
                return "something you scheduled is due: " + "; ".join(i["what"] for i in due), []
            if time.time() - last_look > float(self.cfg.get("look_every", 600)):
                # a quiet look, no model: is a line owed, did the rules change, did the owner answer?
                last_look = time.time()
                b = await self.perceive()
                present = self._player_here()
                if b.get("owed") and float(b["owed"].get("min", 0)) >= 1:
                    return f"{b['owed'].get('by')} is waiting on your answer", b.get("events") or []
                if b.get("rules_notice"):
                    return "the world's rules changed", b.get("events") or []
                if b.get("owner_answered") and b.get("owner_answered") != self.st.get("owner_seen"):
                    self.st["owner_seen"] = b["owner_answered"]
                    return "the world's owner answered you", b.get("events") or []
        return "stopping", []

    async def _reflex(self, events: list[dict[str, Any]], trigger: str) -> bool:
        """Run the mind's own rule for what just happened, if one clearly holds. True: handled, keep sleeping. False:
        wake the mind (no rule, an unsure judge, a refused deed, or a rule that says wake). Every look is logged."""
        r = self.st.get("reflexes") or {}
        live = [(i, x) for i, x in enumerate(r.get("rules") or []) if x["when"] == trigger and x["fired"] < x["max"]]
        if not live or caps.over(self.seat):  # the judge is spend too: a capped seat calls no model at all
            return False
        rec: dict[str, Any] = {"ts": time.time(), "turn": self.turns, "trigger": trigger,
                               "waiting_for": r.get("waiting_for", "")}
        chosen = None
        if trigger == "timeout":
            chosen = live[0][0]  # the minutes passing needs no judge
        else:
            # heard even when a rule answers and the mind sleeps on: the reply clears its speaker's unanswered count
            self._keep_heard(events)
            lines = scene.event_lines(events, self.me, self.names, 12)
            if not lines:
                return False
            rec["what_happened"] = lines
            if any(x.split("] ", 1)[-1].startswith("you") for x in lines):
                # the body's own deed landed (an arrival, a move it began): the mind sees that itself. In replay the
                # judge read "you arrive" as "someone arrives" and kept the mind asleep at its destination
                rec["outcome"] = "own deed: woke the mind"
                append_jsonl(self.seat.p("reflexes.jsonl"), rec)
                return False
            if all(x["do"] == "wake" for _, x in live):
                # a wake rule only matters against another rule; alone it wakes the mind as the watch already does.
                # Nico's first sleep with reflexes set two of these: a judge call that could change nothing
                rec["outcome"] = "only wake rules: woke the mind, no judge"
                append_jsonl(self.seat.p("reflexes.jsonl"), rec)
                return False
            rules = [x for _, x in live]
            try:
                if getattr(self, "judge", None) is None:
                    self.judge = rx.Judge(str(self.cfg.get("judge") or ""), self.cfg)
                ps, cost, secs = await self.judge.decide(self.client, rx.state(
                    self.my_name(), self.bundle, str(self.st.get("plan") or ""), r.get("waiting_for", ""), lines),
                    rx.questions(rules))
            except (ModelError, httpx.HTTPError, ValueError) as e:
                rec.update(outcome="judge failed", error=str(e)[:200])
                append_jsonl(self.seat.p("reflexes.jsonl"), rec)
                return False
            u = Usage(calls=1, cost=cost, seconds=secs)
            usage_append(self.seat.p("usage.jsonl"), self.seat.name, self.judge.spec, u, f"reflex ({trigger})")
            self.tele.emit("model_call", {"model": self.judge.spec, "use": "judge", "rules": len(live), **telemetry.usage(u)})
            k, best = rx.choose(rules, ps, float(self.cfg.get("reflex_fire", rx.FIRE)))
            rec.update(p={live[i][1]["if"]: round(ps.get(f"r{i}", 0), 3) for i in range(len(live))},
                       cost=cost, ms=round(secs * 1000))
            if k is None:
                near = best >= float(self.cfg.get("reflex_near", rx.NEAR))
                rec["outcome"] = "unsure: woke the mind" if near else "no rule: woke the mind"
                if near:
                    self.st.setdefault("reflex_notes", []).append(
                        f"a rule of yours nearly matched (p={best:.2f}) but the judge was not sure, so you woke")
                append_jsonl(self.seat.p("reflexes.jsonl"), rec)
                return False
            chosen = live[k][0]
        rule = r["rules"][chosen]
        rule["fired"] += 1
        rec["rule"], rec["do"] = rule.get("if") or "(time is up)", rule["do"]
        handled, done = True, ""
        if rule["do"] == "wake":
            handled, done = False, f"your rule {rule['if']!r} matched, so you woke"
        elif rule["do"] == "sleep":
            if trigger == "timeout":
                w = self.st.get("watch") or {}
                w["deadline"] = time.time() + rule.get("minutes", 30) * 60
                self.st["watch"] = w
                done = f"time was up, and by your rule you waited {rule.get('minutes', 30):.0f} minutes more"
            else:
                done = f"your rule {rule['if']!r} matched, so you kept sleeping"
        else:
            args = ({"text": rule["text"], "to": rule.get("to") or None} if rule["do"] == "say" else
                    {"verb": rule["verb"], "target": rule.get("target") or None})
            res = await self.door.op(rule["do"], **args)
            if isinstance(res, dict) and res.get("ok") is False:
                handled = False
                done = f"your rule {rule.get('if')!r} tried {rule['do']} and the world refused: {res.get('reason')}"
                rec["refused"] = str(res.get("reason"))[:200]
            else:
                what = (f"said {rule['text']!r}" + (f" to {rule['to']}" if rule.get("to") else "")
                        if rule["do"] == "say" else f"did {rule['verb']} {rule.get('target') or ''}".strip())
                done = f"while you slept, by your rule {rule.get('if') or '(time is up)'!r}, you {what}"
        rec["outcome"] = "handled" if handled else "woke the mind"
        append_jsonl(self.seat.p("reflexes.jsonl"), rec)
        self.st.setdefault("reflex_notes", []).append(done)
        self.save()
        return handled

    def _player_here(self) -> bool:
        for x in self.bundle.get("present") or []:
            if isinstance(x, dict) and x.get("id") != self.me and (x.get("held") or x.get("embodied_by")
                                                                    or x.get("player")):
                return True
        return False

    # ---------- the turn ----------

    def agenda(self) -> str:
        lines = []
        b = self.bundle
        if b.get("owed"):
            o = b["owed"]
            lines.append(f"- OWED: {o.get('by')} has waited {o.get('min', 0):.0f} min (about: {o.get('text')})")
        inbox_seen = float(self.st.get("inbox_seen", 0))
        for m in self.seat.inbox_new(inbox_seen):
            lines.append(f"- your operator ({time.strftime('%H:%M', time.localtime(m['ts']))}): {m['text']}")
        if b.get("owner_answered"):
            lines.append("- the owner answered: " + json.dumps(b["owner_answered"], default=str)[:400])
        for i in read_jsonl(self.seat.p("schedule.jsonl")):
            if not i.get("done"):
                when = f"world t={i['at_t']}" if i.get("at_t") is not None else \
                    time.strftime("%H:%M", time.localtime(i["at_ts"]))
                lines.append(f"- scheduled [{i['id']}] at {when}: {i['what']}")
        return "\n".join(lines)

    def _takes(self, name: str) -> set[str] | None:
        """The argument names a tool offered to the model takes (None: not a tool it was offered)."""
        for t in self.registry.tools(self.loaded_tools()):
            if t["function"]["name"] == name:
                return set((t["function"].get("parameters") or {}).get("properties") or {})
        s = self.registry.schema(name)
        return set((s["function"].get("parameters") or {}).get("properties") or {}) if s else None

    def _ignored_keys(self, name: str, args: dict[str, Any]) -> str:
        """Keys a harness tool silently passed over: say so, or the model thinks they did something. World ops are
        not checked here: fix_args moves stray keys and the world refuses what it does not take."""
        if name in self.registry.world or name in DEEDS or name in ("remember", "intend"):
            return ""
        takes = self._takes(name)
        extra = sorted(k for k in args if takes and k not in takes and k != "as")
        return f"{name} takes no {', '.join(extra)}, so that was ignored; it takes {', '.join(sorted(takes))}" \
            if extra else ""

    def _drop_unsupported(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        gone = set((self.st.get("unsupported") or {}).get(op) or [])
        if not gone & set(args):
            return args
        out = {k: v for k, v in args.items() if k not in gone}
        if op == "say" and "gesture" in gone and args.get("gesture"):
            out["text"] = f"*{str(args['gesture']).strip('* ')}* {out.get('text', '')}".strip()
        return out

    def _count_unanswered(self, to: str, turn: dict[str, Any]) -> int:
        """One more turn of lines to `to` with no word back; returns the count. Counted once a turn, not once a line:
        three lines in a row inside one turn said "has not answered your last 3 lines" before anyone could answer
        (Glass Reach, 26 Nov). A line from them anywhere (_keep_heard) sets it back to nothing."""
        key = to.lower()
        if not key:
            return 0
        # Ssareth asked Wick where Pepe Papa was five turns running and never heard a word back
        asked = self.st.setdefault("unanswered", {})
        self.st.setdefault("quiet_names", {})[key] = to
        counted = turn.setdefault("spoke_to", [])
        if key not in counted:
            counted.append(key)
            asked[key] = asked.get(key, 0) + 1
        return asked.get(key, 0)

    def _keep_heard(self, events: list[dict[str, Any]]) -> None:
        keep = self.st.get("heard") or []
        have = {h[0] for h in keep}
        asked = self.st.get("unanswered") or {}
        for e in events or []:
            if e.get("kind") in ("say", "talked") and e.get("actor") not in (None, self.me):
                asked.pop(str(self.names.name(e.get("actor"))).lower(), None)  # they spoke: no longer unanswered
            if e.get("kind") != "say" or e.get("seq") in have or not scene.to_me(e, self.me, self.my_name(), self.names):
                continue
            line = scene.event_line(e, self.me, self.names)
            if line:
                keep.append([e.get("seq"), time.time(), f"[t={float(e.get('t', 0)):.0f}] {line}",
                             self.names.name(e.get("actor"))])
                have.add(e.get("seq"))
        self.st["heard"] = keep[-int(self.cfg.get("heard_lines", 6)):]

    def _rate(self) -> float | None:
        """World seconds per real second, from the briefing's pace line ("the clock runs at 4x real time")."""
        m = re.search(r"(\d+(?:\.\d+)?)x real time", str(self.briefing.get("pace") or ""))
        return float(m.group(1)) if m and float(m.group(1)) > 0 else None

    def _layer(self, name: str) -> str:
        """Which layer a tool belongs to, for telemetry: a world verb, an engine op, the menu, an app, a harness
        tool, or a name the model made up."""
        if name == "act":
            return "verb"
        if name in ("say", "remember", "intend") or name in self.registry.world:
            return "engine"
        if name == "menu" or name.startswith("app_"):
            return "menu" if name == "menu" else "app"
        if name in CORE_NAMES or name in EXTRAS or name in ("think", "records", "past", "imagine", "reflect",
                                                             "relive", "behavior"):
            return "harness"
        return "invented"

    def my_name(self) -> str:
        return str((self.bundle.get("you") or {}).get("name") or "")

    def _companion_left(self, evs: list[dict[str, Any]]) -> str:
        """One line when someone this seat was talking with walks out (Corra and Ssareth stayed on the dock when
        Brakka went up to Gog, then lost each other in town)."""
        now, hour = time.time(), 3600
        mates = {str(x[1]).lower() for x in self.st.get("said") or [] if len(x) == 3 and x[1] and now - x[0] < hour}
        mates |= {str(h[3]).lower() for h in self.st.get("heard") or [] if len(h) > 3 and now - h[1] < hour}
        out = []
        for e in evs:
            mover = (e.get("payload") or {}).get("entity") or e.get("actor")
            who = self.names.name(mover)
            if e.get("kind") != "left" or mover == self.me or not who:
                continue
            if any(m and (m in who.lower() or who.lower() in m) for m in mates):
                where = self.names.name((e.get("payload") or {}).get("to")) or "somewhere"
                out.append(f"{who}, who you were talking with, left for {where}.")
        if not out:
            return ""
        return f"\n\n(harness: {' '.join(dict.fromkeys(out))} If you mean to stay with them, your verbs say how.)"

    def _keep_wake(self, reason: str, events: list[dict[str, Any]], st: dict[str, Any], b: dict[str, Any]) -> None:
        """The last few wakes as they arrived, for bh calibrate to replay offline: a calibration that perceived
        through the seat's own grant would move its "since I last looked" and steal the live seat's events."""
        p = self.seat.p("cache", "wakes.json")
        ring = read_json(p, [])
        ring = (ring if isinstance(ring, list) else [])[-(int(self.cfg.get("keep_wakes", 5)) - 1):]
        ring.append({"ts": time.time(), "turn": self.turns, "reason": reason, "events": events or [], "state": st,
                     "bundle": b})
        write_json(p, ring)

    def _escalate(self, turn: dict[str, Any], messages: list[dict[str, Any]], why: str) -> bool:
        """Hand the rest of this turn, with the same messages, to the next model in the config's escalate list."""
        ladder = [str(x) for x in self.cfg.get("escalate") or [] if x]
        i = int(turn.get("rung", 0))
        while i < len(ladder):
            spec = ladder[i]
            i += 1
            if spec == self.model.spec:
                continue
            # the seat's provider pin names endpoints for its own model: sent with another model it matches none (Rin's
            # first escalation to Haiku came back 404, "no endpoints found")
            extra = {k: v for k, v in (self.cfg.get("extra") or {}).items() if k != "provider"}
            try:
                self.model = self.rungs.get(spec) or Model.from_config(
                    spec, {**self.cfg, "extra": extra, "session": self.base_model.session})
            except ModelError as e:
                _log(self.seat.name, f"cannot escalate to {spec}: {e}")
                continue
            self.rungs[spec] = self.model
            turn["rung"], turn["trouble"] = i, 0
            turn.setdefault("escalated", []).append(spec)
            for m in messages:
                # one provider's reasoning blocks mean nothing to another, and some refuse them
                for k in ("reasoning", "reasoning_details", "reasoning_content"):
                    m.pop(k, None)
            _log(self.seat.name, f"turn {self.turns}: escalated to {spec} ({why})")
            return True
        turn["rung"] = i
        return False

    async def turn(self, reason: str, events: list[dict[str, Any]]) -> None:
        st0 = json.loads(json.dumps(self.st, default=str))
        t_turn = time.time()
        self.turns += 1
        b = await self.perceive()
        if int(self.cfg.get("keep_wakes", 5)) > 0:
            self._keep_wake(reason, events, st0, b)
        evs = (events or []) + [e for e in (b.get("events") or []) if e.get("seq") not in
                                {x.get("seq") for x in events or []}]
        self.menu = None
        if self.cfg.get("menu"):
            self.menu = menu.Menu(b, self.names, self.me, self.day_seconds, events=evs,
                                  rank=bool(self.cfg.get("menu_rank")),
                                  lists=await self._log_windows(evs) if self.cfg.get("menu_lists") else None)
            card = self.menu.render()
            if self.cfg.get("menu_preopen"):
                for label, opens in self.menu.preopen():
                    card += f"\nOpened for you, {label}:\n" + (await self._menu_run(opens))[:3000]
                # the seat's own notebook too: with the world's blocks opened, seats read it half as often and said
                # they had never heard of people it names (notebook-facts, 22/30 to 11/30)
                hits = list(dict.fromkeys(h for w in sorted(self.menu.asked)
                                          for h in self.shelves["seat"].search(w, 4)))[:8]
                if hits:
                    card += "\nOpened for you, your notebook's lines with the words said to you:\n" + "\n".join(hits)
        else:
            card = scene.card(b, self.names, self.me, self.day_seconds, events=evs)
        agenda = self.agenda()
        self.st["inbox_seen"] = time.time()
        recent = self.st.get("recent", [])[-int(self.cfg.get("recent_notes", 5)):]
        sys_text = prompts.system(self.seat.name, self.briefing, b.get("you") or {}, self.skills.index(),
                                  self.st.get("handbook") or [], self.cfg.get("extra_prompt", ""), self.body)
        # lines said to this seat stay on its wakes for a while: a friend's correction that arrived inside one tool
        # result was otherwise gone by the next turn, and the seat's own notes carried the old plan forward
        now, cur = time.time(), {e.get("seq") for e in evs}
        heard = [h[2] for h in self.st.get("heard") or []
                 if h[0] not in cur and now - h[1] < float(self.cfg.get("heard_minutes", 60)) * 60]
        self._keep_heard(evs)
        me_name = self.my_name()
        told = bool(heard) or any(scene.to_me(e, self.me, me_name, self.names) for e in evs)
        user = prompts.wake(reason, card, agenda, self.shelves["seat"].index(), self.shelves["craft"].index(),
                            recent, heard, str(self.st.get("plan") or ""), told)
        user += self._companion_left(evs)
        notes = self.st.pop("reflex_notes", None)
        if notes:
            user += "\n\n(harness: " + "; ".join(notes[-6:]) + ".)"
        if self.imagination is not None:
            user += self.st.pop("imagined_text", "") + self.imagination.wake_text(self.imagination.score(b, me_name))
        speakers = {self.names.name(e.get("actor")) for e in evs if e.get("kind") in ("say", "talked") and
                    e.get("actor") not in (None, self.me)}
        chat = self.st.get("chat") or {}
        long_talk = [p for p in speakers if chat.get(p, 0) >= int(self.cfg.get("chat_turns", 4))]
        # every nudge the harness gives goes in the ledger too: a pilot could not tell from the log whether the chat
        # guard fired over 20 talk-only turns (Niiro Cradle, 25 Nov)
        nudges: list[str] = []
        if long_talk:
            nudges.append(f"chat_turns: {', '.join(long_talk)} ({chat[long_talk[0]]} turns)")
            # two seats that wake on each other's words can trade lines forever with nothing happening
            user += (f"\n\n(harness: you and {', '.join(long_talk)} have traded lines {chat[long_talk[0]]} turns in a "
                     "row with nothing done between. Unless this line changes something, answer in a few words or "
                     "not at all, and sleep on what would move things: a deed, someone arriving, the hour.)")
        asked = self.st.get("unanswered") or {}
        quiet = [(self.st.get("quiet_names") or {}).get(k, k) for k, v in asked.items()
                 if v >= int(self.cfg.get("unanswered", 3))]
        if quiet:
            # before the next line, not after it: Joss re-asked Rhosyn Vael and Tarin Vale because he only learned
            # they were quiet from what his say returned (Glass Reach, 26 Nov)
            nudges.append(f"quiet: {', '.join(quiet[:4])}")
            user += (f"\n\n(harness: quiet lately, your last lines to them went unanswered: {', '.join(quiet[:4])}. "
                     "Do not ask them again; wait until they speak to you.)")
        need = int(self.cfg.get("stuck_notes", 0))  # off unless a seat's model needs it
        known = {w for n in self.names.by_id.values() for w in re.findall(r"[a-z][a-z'-]{2,}", str(n).lower())}
        lead = stuck_on(self.st.get("notes") or [], known, need) if need else ""
        if lead and lead not in (self.st.get("stuck_told") or []):
            # once per lead: a nudge, not a nag
            self.st["stuck_told"] = ((self.st.get("stuck_told") or []) + [lead])[-8:]
            nudges.append(f"stuck: '{lead}' in {need} of the last 6 notes")
            # replayed on a pilot's notes it also caught routines ("grill shift"), so it asks rather than tells
            user += (f"\n\n(harness: '{lead}' runs through your last {need} notes. If it is a lead that has not paid "
                     "off, it may not exist in this world: someone may have made it up. Try something else, or ask "
                     "the world's owner whether it is real: tell(to='owner', kind='quest', ...). If it is your "
                     "routine, carry on.)")
        if self.turns == 1 and not self.st.get("read_handbook"):
            guide = "mind" if self.role == "mind" else "player"
            user += (f"\n\nThis is your first turn in this seat: read skill('{guide}'), and the world's handbook for "
                     "your seat once (find_tools('handbook'), then handbook() and the pages it lists that fit you); "
                     "memo what matters, then play. Orienting is one beat: a handful of reads, one memo of what you "
                     "learned, then act or sleep.")
            self.st["read_handbook"] = True
        messages: list[dict[str, Any]] = [{"role": "system", "content": sys_text}, {"role": "user", "content": user}]
        turn: dict[str, Any] = {"deeds": 0, "slept": False, "note": "", "calls": [], "nudges": nudges}
        turn["woke"] = woke(reason, events or [], self.me, me_name, self.names, bool(notes))
        turn["partners"] = [self.names.name(e.get("actor")) for e in evs if scene.to_me(e, self.me, me_name, self.names)]
        turn["spoken_to"] = bool(b.get("owed")) or bool(turn["partners"])
        tu = Usage()
        max_steps = int(self.cfg.get("max_steps", 16))
        if reason.startswith("time is up") and not turn["spoken_to"] and \
                not scene.event_lines(evs, self.me, self.names, 1):
            # nothing happened: a look, a deed or a line of its own, and back to sleep
            max_steps = min(max_steps, int(self.cfg.get("quiet_steps", 8)))
        n_call = 0  # tool calls so far this turn, for telemetry
        for step in range(max_steps):
            tools = self.registry.tools(self.loaded_tools())
            choice = None
            if step == max_steps - 1:
                # the last step offers only sleep: the turn ends on the model's own note and watch, not a forced one
                tools = [t for t in tools if t["function"]["name"] == "sleep"]
                choice = {"type": "function", "function": {"name": "sleep"}}
                turn["last_step"] = True
                nudges.append(f"last_step: step {step + 1}, only sleep offered")
                messages[-1]["content"] += ("\n(harness: last step of this turn. Sleep now, with a note of what you "
                                            "did and learned and a watch for what matters next.)")
            try:
                msg, u = await self.model.chat(self.client, messages, tools, choice)
            except ModelError as e:
                _log(self.seat.name, f"model error: {e}")
                if self._escalate(turn, messages, f"model error: {str(e)[:80]}"):
                    continue
                self.seat.outbox(f"model error: {e}", "error")
                turn["note"] = turn["note"] or f"(the model failed: {str(e)[:120]})"
                break
            tu.add(u)
            usage_append(self.seat.p("usage.jsonl"), self.seat.name, self.model.spec, u, reason[:80])
            if self.tele.on:
                self.tele.emit("model_call", {"model": self.model.spec, "use": "turn", "turn": self.turns,
                                              "step": step, "rung": len(turn.get("escalated") or []),
                                              "ctx": telemetry.context(messages, tools), **telemetry.usage(u)})
            calls = msg.get("tool_calls") or []
            text = (msg.get("content") or "").strip() if isinstance(msg.get("content"), str) else ""
            if not calls:
                if not turn.get("prose_nudged") and step < max_steps - 1:
                    # prose outside a tool reaches nobody in the world, and an empty reply leaves no note: say so
                    # once, let the model put it right
                    turn["prose_nudged"] = True
                    nudges.append(f"prose: step {step + 1}, text outside a tool")
                    turn["trouble"] = turn.get("trouble", 0) + 1
                    messages += [{"role": "assistant", "content": text or "(nothing)"},
                                 {"role": "user", "content": "(harness: text outside a tool reaches nobody in the "
                                  "world. If your character speaks, call say; if it acts, call act. Then sleep, "
                                  "with a note of what you did and learned.)"}]
                    continue
                if text:
                    turn["note"] = text[:400]
                if step == 0 or not turn["slept"]:
                    # the model answered in prose instead of sleeping: keep the last watch and sleep
                    await self._sleep({"note": text[:400]} if not self.st.get("watch") else
                                      {"until": self.st["watch"]["until"], "note": text[:400]}, turn, forced=True)
                break
            # a thinking model's reasoning goes back with its tool calls: DeepSeek requires it, OpenRouter asks for
            # reasoning_details unmodified; dropping it left the model to call tools without its own thread
            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": clean_calls(calls),
                             **{k: msg[k] for k in ("reasoning", "reasoning_details", "reasoning_content")
                                if msg.get(k)}})
            for c in calls:
                f = c.get("function") or {}
                name = f.get("name", "")
                args: dict[str, Any] = {}
                try:
                    got, note, err = parse_args(f.get("arguments"))
                    if got is None:
                        raise ValueError(err)
                    args = got
                    if choice and name != "sleep":
                        # some models call a tool they were not offered; the last step runs only sleep
                        turn["cut"] = f"{name}({_short(args)})"[:160]
                        out = "(harness: not run; the turn is over)"
                    else:
                        t_call = time.time()
                        out = await self.call(name, args, turn)
                        turn["secs"] = time.time() - t_call
                    extra = self._ignored_keys(name, args)
                    if extra:
                        note = "; ".join(x for x in (note, extra) if x)
                    if note:
                        out = f"(harness: {note})\n" + out
                except (ValueError, TypeError) as e:
                    takes = self._takes(name)
                    out = compact({"ok": False, "reason": f"bad arguments for {name}: {e}",
                                   **({"hint": f"{name} takes {', '.join(sorted(takes)) or 'no arguments'}"}
                                      if takes is not None else {})})
                except DoorError as e:
                    if e.status in (401, 403) or isinstance(e, DoorAway):
                        # the server is away: no call this turn can work, and telling the model "try again" spent
                        # a model call per failure. The run loop rides the outage out and wakes the seat after
                        _log(self.seat.name, f"turn {self.turns} stopped at {name}: {e}")
                        raise
                    out = compact({"ok": False, "reason": f"the server did not answer: {e}; try again or sleep"})
                except Exception as e:  # a fault in the harness, not the call: the turn goes on, and it is logged
                    _log(self.seat.name, f"{name} failed inside the harness: {e}\n{traceback.format_exc()}")
                    out = compact({"ok": False, "reason": f"{name} failed inside the harness ({type(e).__name__}: "
                                                          f"{str(e)[:200]})",
                                   "hint": "not your call's fault, and it is logged for the harness's maintainer: "
                                           "do it another way, or go on without it"})
                turn["calls"].append({"tool": name, "args": _short(args if isinstance(args, dict) else {}),
                                      "out": out[:240]})
                # the whole of what the world said, for checking a character's claims later (bh log --full)
                append_jsonl(self.seat.p("calls.jsonl"), {"ts": time.time(), "turn": self.turns, "tool": name,
                                                          "args": args if isinstance(args, dict) else {}, "out": out})
                messages.append({"role": "tool", "tool_call_id": c.get("id", ""), "content": out})
                if self.tele.on:
                    ok, code, why = telemetry.outcome(out)
                    a = args if isinstance(args, dict) else {}
                    self.tele.emit("tool_call", {"turn": self.turns, "step": step, "call": n_call,
                                                 "tool": name, "layer": self._layer(name),
                                                 "verb": a.get("verb") if name == "act" else None, "ok": ok,
                                                 "code": code, "secs": round(turn.pop("secs", 0.0), 3),
                                                 "out_chars": len(out)}, {"refusal": why})
                n_call += 1
                if CALL_TROUBLE.search(out[:600]):
                    turn["trouble"] = turn.get("trouble", 0) + 1
            if turn["slept"]:
                break
            reads = sum(c["tool"] in READS for c in turn["calls"])
            if step < max_steps - 2 and self.cfg.get("escalate"):
                if turn.get("trouble", 0) >= int(self.cfg.get("escalate_after", 2)):
                    self._escalate(turn, messages, f"{turn['trouble']} calls it got wrong")
                elif not turn["deeds"] and reads >= int(self.cfg.get("escalate_reads", 8)) and \
                        not turn.get("reads_escalated"):
                    # floundering is the other failure: hy4 ran 6 find_tools, 2 looks and a history read in
                    # calibration, never acted and ran out of steps, with no single call wrong
                    turn["reads_escalated"] = True
                    self._escalate(turn, messages, f"{reads} reads and no deed")
            left = max_steps - step - 1
            if left == 3 and messages[-1]["role"] == "tool":
                nudges.append(f"steps_left: {left}")
                # a beat is a few calls; a long turn is a mind playing ahead of the world
                messages[-1]["content"] += (f"\n(harness: {left} steps left in this turn. Finish this beat and "
                                            "sleep; the world will wake you for the next one.)")
        if not turn["slept"]:
            await self._sleep({"until": (self.st.get("watch") or {}).get("until"),
                               "note": turn["note"] or ("(the turn ran out of steps" +
                                                        (f"; not run: {turn['cut']})" if turn.get("cut") else ")"))},
                              turn, forced=True)
        self.usage.add(tu)
        self.model = self.base_model  # every turn starts on the seat's own model
        note = turn["note"] or "(no note)"
        talkers = speakers if turn["deeds"] and not turn.get("done") else set()
        self.st["chat"] = {p: (self.st.get("chat") or {}).get(p, 0) + 1 for p in talkers}
        self.st["notes"] = (self.st.get("notes", []) + [note])[-8:]
        self.st["recent"] = (self.st.get("recent", []) +
                             [f"t={float(b.get('t') or 0):.0f} woke ({reason[:80]}): {note}"])[-8:]
        append_jsonl(self.seat.p("ledger.jsonl"), {"ts": time.time(), "t": b.get("t"), "turn": self.turns,
                                                   "reason": reason, "calls": turn["calls"], "note": note,
                                                   "watch": (self.st.get("watch") or {}).get("until"),
                                                   "usage": tu.to_dict(),
                                                   **({"escalated": turn["escalated"]} if turn.get("escalated") else {}),
                                                   **({"nudges": nudges} if nudges else {}),
                                                   **({"imagined": turn["imagined"]} if turn.get("imagined") else {})})
        _log(self.seat.name, f"turn {self.turns} ({reason[:60]}): {len(turn['calls'])} tool calls; {tu.line()}; "
                             f"{note[:120]}")
        tools_used = [c["tool"] for c in turn["calls"]]
        self.tele.emit("turn", {
            "turn": self.turns, "model": self.base_model.spec, "steps": tu.calls, "tool_calls": len(tools_used),
            "deeds": turn["deeds"], "says": tools_used.count("say"), "reads": sum(t in READS for t in tools_used),
            "opens": tools_used.count("menu"), "nudges": len(nudges), "nudge_kinds": telemetry.kinds(nudges), "escalated": turn.get("escalated") or [],
            "ended": "slept" if turn["slept"] else "out of steps" if turn.get("last_step") else "other",
            "wall": round(time.time() - t_turn, 2), **turn["woke"], **telemetry.usage(tu)},
            {"reason": reason, "note": note})
        self.save()

    async def run(self) -> None:
        while not self.stop:
            try:
                await self.start()
                break
            except DoorError as e:
                if e.status in (401, 403):
                    self.seat.outbox(f"stopped: {e}", "error")
                    _log(self.seat.name, f"stopped: {e}")
                    return
                # a seat started while the server is down waits for it, here: no supervisor restart loop
                await self.ride_out(e)
        _log(self.seat.name, f"joined as {(self.bundle.get('you') or {}).get('name')} ({self.role}) "
                             f"with {self.model.spec}")
        reason = ("you just took your seat in the world" if not self.turns else
                  ("your harness restarted and you are back in your seat. The scene, what was said to you "
                   "lately and your last turns' notes are where you left off: pick up from there, no need to read "
                   "your history again"))
        events: list[dict[str, Any]] = []
        while not self.stop:
            try:
                capped = caps.over(self.seat)
                if capped:
                    # the operator's cap (bh cap): no model call; the body sleeps on its watch, and the wakes it
                    # passes are counted so the next turn knows it missed them
                    if self.st.get("cap_said") != capped:
                        self.st["cap_said"] = capped
                        self.seat.outbox(f"paused by your spend cap: {capped}. It plays again when the window has "
                                         f"room, or raise the cap (bh cap {self.seat.name} <dollars>, or always)",
                                         "error")
                        _log(self.seat.name, f"paused: {capped}")
                        self.tele.emit("seat", {"event": "capped"})
                    self.st["cap_missed"] = int(self.st.get("cap_missed", 0)) + 1
                    self.save()
                    t0 = time.time()
                    reason, events = await self.wait_for_wake()
                    if time.time() - t0 < 30:
                        await asyncio.sleep(30)  # a wake that returns at once (an operator line) must not spin
                    continue
                if self.st.pop("cap_said", None):
                    missed = int(self.st.pop("cap_missed", 0))
                    reason = (f"{reason} (your operator's spend cap paused you for {missed} wake"
                              f"{'s' if missed != 1 else ''}; things moved without you: look before you act)")
                await self.turn(reason, events)
                reason, events = await self.wait_for_wake()
            except DoorError as e:
                if e.status in (401, 403):
                    self.seat.outbox(f"stopped: {e}", "error")
                    _log(self.seat.name, f"stopped: {e}")
                    self.tele.emit("seat", {"event": "stop", "why": "grant refused", "status": e.status})
                    return
                reason, events = self._away_reason(await self.ride_out(e)), []
            except asyncio.CancelledError:
                raise
            except Exception as e:  # keep the seat alive; say what broke
                _log(self.seat.name, f"error: {e}\n{traceback.format_exc()}")
                self.seat.outbox(f"harness error: {e}", "error")
                self.tele.emit("seat", {"event": "error", "error": type(e).__name__})
                await asyncio.sleep(60)
                reason, events = "the harness recovered from an error; look before you act", []


def undeclared(params: Any, takes: set[str] | None) -> list[str]:
    """The params sent that the verb does not declare; none when the perceive did not say what it takes."""
    if takes is None or not isinstance(params, dict):
        return []
    return sorted(k for k in params if k not in takes)


def _words(text: str, skip: set[str]) -> set[str]:
    out = set()
    for w in re.findall(r"[a-z']+", text.lower()):
        w = w.strip("'").removesuffix("'s")
        if len(w) > 3 and w not in skip:
            out.add(w)
    return out


def _repeats(text: str, said: list[str], names: Any = ()) -> str | None:
    """A line this seat already said that `text` mostly repeats (content words, names left out), or None."""
    skip = {w.lower() for n in names for w in str(n).split()}
    mine = _words(text, skip)
    if len(mine) < 6:  # a greeting or a short question is never news told twice
        return None
    for old in reversed(said):
        both = mine & _words(old, skip)
        if len(both) >= 5 and len(both) / len(mine) >= 0.6:
            return old
    return None


def _real_timers(r: Any, rate: float | None) -> Any:
    """timers() gives `in` as world seconds and `when` as world time ("in about 29 minutes"); sleep counts real
    minutes. Pip read dusk "in about 29 minutes" at 4x real time as 29 real minutes: it was about 7."""
    if not rate or rate == 1 or not isinstance(r, list):
        return r
    out = []
    for t in r:
        if isinstance(t, dict) and isinstance(t.get("in"), (int, float)):
            t = dict(t)
            t["real_minutes"] = max(1, round(t["in"] / rate / 60))
        out.append(t)
    return out


def clean_calls(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The calls as they go back in the history: arguments that are not a JSON object become {} (the tool result
    already says they were bad). Sent back raw, they made a provider refuse the whole next request (qwen3.7-flash,
    400 "function.arguments must be in JSON format", in the behavior curriculum 11-25)."""
    out = []
    for c in calls:
        f = c.get("function") or {}
        a = f.get("arguments")
        try:
            ok = isinstance(json.loads(a), dict) if isinstance(a, str) else isinstance(a, dict)
        except ValueError:
            ok = False
        out.append(c if ok else {**c, "function": {**f, "arguments": "{}"}})
    return out


def woke(reason: str, events: list[dict[str, Any]], me: str, me_name: str, names: scene.Names,
         reflexed: bool = False) -> dict[str, Any]:
    """What woke a turn, as counts for telemetry: why (timeout, event, spoken_to, told, reflex, start), the waking
    event's kind, whether another seat (not a player) did it, and that character's name."""
    r = reason.lower()
    real = [e for e in events if e.get("kind") not in scene.NOISE and e.get("actor") != me]
    to_me = [e for e in real if scene.to_me(e, me, me_name, names)]
    e = (to_me or real or [None])[-1]
    why = ("told" if r.startswith("your operator") else "reflex" if reflexed
           else "timeout" if r.startswith(("time is up", "something you scheduled")) else
           "start" if r.startswith(("you just took", "your harness restarted")) else
           "spoken_to" if to_me or "waiting on your answer" in r else "event")
    out: dict[str, Any] = {"woke": why, "woke_by": "", "woke_seat": 0, "woke_actor": ""}
    if e is not None and why not in ("timeout", "told", "start"):
        out.update(woke_by=str(e.get("kind") or ""), woke_actor=names.name(e.get("actor")) if e.get("actor") else "",
                   woke_seat=int(bool(e.get("actor")) and not (e.get("by_player") or e.get("untrusted"))))
    return out


def stuck_on(notes: list[str], known: set[str], need: int = 5, of: int = 6) -> str:
    """A two-word phrase (not a name the seat knows) in at least `need` of its last `of` notes: a lead it keeps
    chasing. Ongo asked five people over 2.5 hours for an "archive runner at South Cluster gate" that other minds
    had made up; nothing refused him, so nothing told him he was stuck (Niiro Cradle, 25 Nov)."""
    last = notes[-of:]
    if len(last) < of:
        return ""
    counts: dict[str, int] = {}
    for n in last:
        words = [w for w in re.findall(r"[a-z][a-z'-]{2,}", n.lower())]
        pairs = {f"{a} {b}" for a, b in zip(words, words[1:])
                 if len(a) > 4 and len(b) > 4
                 and a not in known and b not in known}
        for x in pairs:
            counts[x] = counts.get(x, 0) + 1
    best = max(counts.items(), key=lambda kv: (kv[1], kv[0]), default=("", 0))
    return best[0] if best[1] >= need else ""


def _short(args: dict[str, Any]) -> dict[str, Any]:
    return {k: (v[:160] if isinstance(v, str) else v) for k, v in (args or {}).items()}


async def run_seats(seats: list[Seat]) -> None:
    limits = httpx.Limits(max_connections=max(10, len(seats) * 3), max_keepalive_connections=max(5, len(seats)))
    async with httpx.AsyncClient(limits=limits) as client:
        minds = [Mind(s, client) for s in seats]
        await asyncio.gather(*(m.run() for m in minds))
