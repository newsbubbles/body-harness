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

from . import imagine, imports, past, prompts, sandbox, scene
from .door import Door, DoorError
from .memory import Shelf, memo
from .providers import Model, ModelError
from .seat import Seat, append_jsonl, read_json, read_jsonl, write_json
from .skills import Skills
from .tools import EXTRAS, Registry, compact, fix_args, route
from .usage import Usage, append as usage_append

DEEDS = {"act", "say", "gesture", "step", "cross", "act_many"}
# a call the model got wrong, not a world that said no: these move a turn up the escalate list (a rule refusal, like
# a gangplank that will not take an elf, is the game and never escalates). The engine's layer="call" says it outright.
CALL_TROUBLE = re.compile(r"bad arguments|there is no tool|\bno tool \w|unexpected keyword argument|missing \d+ "
                          r"required|syntax error|Not asleep yet|no verb '|\"layer\": \"call\"")
READS = {"look", "find_tools", "inspect", "find", "history", "guide", "handbook", "recall", "skill", "owed", "agenda",
         "past"}
KEEP_LOADED_TURNS = 5
MAX_LOADED = 8


# Words that claim a deed, what they claim, and the verbs that would do it (the first the world has is suggested)
_CLAIMS = [
    (r"\b(i take (it|the job|the work|your offer)|i('ll| will) take (it|the job|the work)|accept(ed)?\b.{0,40}\b(job|"
     r"offer|work)|took\b.{0,30}\bjob|i('m| am) in for the job)\b", "took the job",
     ("take_job", "accept", "hire")),
    (r"\b(i give (you|him|her|them)|i('ve| have) given|gave (you|him|her|them)|here('s| is) (the|your) "
     r"(coin|gold|silver|money))\b", "gave it", ("give", "pay")),
    (r"\b(i bought|i('ve| have) bought|i('ll| will) buy (it|that|this) now|paid (you|him|her|the))\b", "paid",
     ("buy", "pay", "give")),
]


def _brief(args: dict[str, Any]) -> str:
    return ", ".join(f"{k}={v!r}" for k, v in args.items() if v not in (None, "", {}))[:160]


# the engine's refusals for a limited grant's reach (its under, types, scripts): another hand may cover it
_OUT_OF_REACH = re.compile(r"your grant|start with|the door refused this grant", re.I)
_TARGET_KEYS = ("entity", "parent", "thing", "target", "to", "into", "under")


def _target_of(args: dict[str, Any]) -> str:
    return next((str(args[k]) for k in _TARGET_KEYS if isinstance(args.get(k), str) and args.get(k)), "")


def _log(seat: str, msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {seat}: {msg}", flush=True)


class Mind:
    def __init__(self, seat: Seat, client: httpx.AsyncClient):
        self.seat = seat
        self.client = client
        self.cfg = seat.config()
        self.door = Door.load(seat.p("door.json"), client)
        self.door.wait_cap = float(self.cfg.get("wait_cap", self.door.wait_cap))
        # extra grants on the same world (a builder's hand on one part of it): bh hand SEAT add NAME <invite>
        self.hands: dict[str, dict[str, Any]] = {
            f.stem: {"door": Door.load(f, client), "ops": set(), "role": ""}
            for f in (sorted(seat.p("hands").glob("*.json")) if seat.p("hands").is_dir() else [])}
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
        return [n for n in names if isinstance(n, str) and n]

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
        if isinstance(b, dict) and b.get("ok") is False and "join" in str(b.get("reason", "")).lower():
            await self.join()
            b = await self.door.op("perceive", events=40)
        self._keep_bundle(b)
        return b

    async def start(self) -> None:
        await self.join()
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
        if self.cfg.get("past") and self.me and not self.seat.p(past.WORLD_FILE).exists():
            # a body that lived before this harness ran it keeps its deeds searchable: the world's log is the source
            try:
                _log(self.seat.name, "past: " + await past.import_world(self.door, self.me, self.names.by_id,
                                                                        self.seat.dir))
            except (DoorError, OSError, ValueError) as e:
                _log(self.seat.name, f"past: could not read the world's history ({e}); bh past-import tries again")
        self.save()

    # ---------- tools ----------

    def _hands_for(self, name: str, args: dict[str, Any]) -> list[str]:
        """The hands to try for an op the mind's grant lacks: the one that last did a deed on this target first."""
        hs = [h for h, hand in self.hands.items() if name in hand["ops"]]
        last = (self.st.get("hand_for") or {}).get(_target_of(args))
        return sorted(hs, key=lambda h: h != last)

    async def _op(self, name: str, args: dict[str, Any]) -> Any:
        """One world op, through the mind's door, or for an author op through the hand that covers the target. A
        hand whose reach the target is outside refuses, and the next hand is tried; the refusal names each one."""
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

    async def call(self, name: str, args: dict[str, Any], turn: dict[str, Any]) -> str:
        if name == "look":
            if args.get("part"):
                if args["part"].strip().lower() in ("affordances", "you can", "verbs"):
                    return "\n".join(scene.affordance_lines(self.bundle.get("affordances") or [], True)
                                     or [str(v) for v in (self.bundle.get("verbs") or [])])
                return compact(scene.part(self.bundle, args["part"]), 8000)
            b = await self.perceive()
            return scene.card(b, self.names, self.me, self.day_seconds)
        if name == "sleep":
            return await self._sleep(args, turn)
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
            if meant and (meant[0] != "act" or meant[1]["verb"] in self.verb_names() or not self.verb_names()):
                out = await self.call(meant[0], meant[1], turn)
                return f"(harness: there is no tool {name}; read as {meant[0]}({_brief(meant[1])}))\n" + out
            known = [t["function"]["name"] for t in self.registry.tools(self.loaded_tools())] + sorted(self.verb_names())
            near = difflib.get_close_matches(name, known, n=3, cutoff=0.6)
            return compact({"ok": False, "reason": f"no tool {name}",
                            "hint": (f"did you mean {', '.join(near)}? " if near else "") +
                                    "find_tools(query) finds and loads tools; verbs go through act(verb, ...)"})
        fixed, notes = fix_args(name, args, self)
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
            told = None if turn.get("hearsay_ok") == text else \
                _hearsay(text, self._voices(), [self.names.by_id.get(i) for i in self.names.people if i != self.me])
            if told:
                turn["hearsay_ok"] = text  # the same line again goes through: the seat stands by it
                last = f" The last you heard from them: {told[2]!r}." if told[2] else \
                    " You have heard no line from them lately."
                return compact({"ok": False, "reason": f"not said: your line gives {told[0]} words ({told[1]!r}) that "
                                                       f"nothing you heard from them carries.{last}",
                                "hint": "say only what they said, name who you really heard it from, or say it as "
                                        "your own guess. If they truly said it, say the same line again"})
        if name == "away":
            self.st["away"] = fixed
        fixed = self._drop_unsupported(name, fixed)
        r = await self._op(name, fixed)
        bad = re.search(r"unexpected keyword argument '(\w+)'", str(r)) if isinstance(r, dict) else None
        if bad and bad.group(1) in fixed:
            # an older engine has no such parameter (Klhyts Refuge's say takes no gesture): drop it for this seat
            # from now on and send the call again, rather than spend the model's step on a refusal
            self.st.setdefault("unsupported", {}).setdefault(name, []).append(bad.group(1))
            fixed = self._drop_unsupported(name, fixed)
            r = await self._op(name, fixed)
        if isinstance(r, dict) and r.get("ok") is False and "join" in str(r.get("reason", "")).lower() \
                and "seat" in str(r.get("reason", "")).lower():
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
        if ok and name == "say":
            claim = self._deed_claim(str(fixed.get("text", "")), turn)
            if claim:
                notes.append(f"your words say you {claim[0]}, but no deed did it, and words do not move the world. "
                             f"To do it: act(verb='{claim[1]}', ...)")
        if ok and name in ("say", "act") and (fixed.get("to") or (name == "act" and fixed.get("verb") == "talk")):
            who = fixed.get("to") or fixed.get("target")
            if isinstance(who, str) and who:
                turn.setdefault("partners", []).append(who)
        if ok and name == "say":
            self.st["said"] = (self.st.get("said", []) + [[time.time(), str(fixed.get("to") or ""),
                                                           str(fixed.get("text", ""))]])[-8:]
            to = str(fixed.get("to") or "").lower()
            if to:
                # Ssareth asked Wick where Pepe Papa was five turns running and never heard a word back
                asked = self.st.setdefault("unanswered", {})
                asked[to] = asked.get(to, 0) + 1
                if asked[to] >= int(self.cfg.get("unanswered", 3)):
                    notes.append(f"{fixed['to']} has not answered your last {asked[to]} lines to them. Some bodies "
                                 f"have no mind behind them and never answer: ask someone else, look for another "
                                 f"way, or stop waiting on them")
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

    async def _tell(self, a: dict[str, Any]) -> Any:
        what, why = str(a.get("what", "")).strip(), str(a.get("why", "") or "").strip()
        if a.get("to") == "operator":
            self.seat.outbox(what + (f"\n{why}" if why else ""), a.get("kind") or "note")
            _log(self.seat.name, f"to operator: {what[:200]}")
            return {"ok": True, "sent": "to your operator"}
        kind = a.get("kind") or "other"
        if kind in ("flaw", "vulnerability"):
            return await self.door.op("report_flaw", what=what, how=why, severity=a.get("severity") or "medium")
        kind = {"feature": "capability"}.get(kind, kind)
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
            claim = None if turn.get("claim_checked") else self._deed_claim(str(a.get("note") or ""), turn)
            if claim:
                turn["claim_checked"] = True
                return (f"Not asleep yet: your note says you {claim[0]}, but no act did it this turn, so the world "
                        f"does not know. Do it now (act(verb='{claim[1]}', ...)), or write the note as what you said, "
                        f"not what you did, and sleep.")
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
        lost_since = None
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
                return f"time is up on your watch ({until!r} did not happen)", []
            # short pieces so the operator's word and real-time schedules land within half a minute; one light
            # request per piece per seat. (A wait is never cancelled mid-flight: the server would move the cursor
            # past whatever fired.)
            piece = float(self.cfg.get("wait_piece", 30))
            step = min(self.door.wait_cap, piece, watch["deadline"] - now, (due_real - now) if due_real else 1e9)
            try:
                w = await self.door.wait(until, max(5.0, step), present=present or None)
                lost_since = None
            except DoorError as e:
                if e.status in (401, 403):
                    raise
                lost_since = lost_since or now
                _log(self.seat.name, f"lost the door ({e}); trying again")
                await asyncio.sleep(30)
                continue
            if isinstance(w, dict) and w.get("ok") is False:
                reason = str(w.get("reason", ""))
                if "join" in reason.lower() or "seat" in reason.lower() or "embod" in reason.lower():
                    _log(self.seat.name, f"seat lapsed ({reason[:80]}); joining again")
                    await self.join()
                    continue
                return f"your watch was refused: {reason} ({w.get('hint', '')}); set a new one", []
            if w.get("fired"):
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

    def _deed_claim(self, text: str, turn: dict[str, Any]) -> tuple[str, str] | None:
        """(what the words claim, the verb that would do it) when a line or note claims a deed no act did this turn
        and the world has the verb. Ssareth told Gog "I take it" and never ran take_job."""
        verbs, done = self.verb_names(), turn.get("verbs") or set()
        for pat, what, cands in _CLAIMS:
            if re.search(pat, text, re.I):
                verb = next((v for v in cands if v in verbs), None)
                if verb and not (done & set(cands)):
                    return what, verb
        return None

    def _drop_unsupported(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        gone = set((self.st.get("unsupported") or {}).get(op) or [])
        if not gone & set(args):
            return args
        out = {k: v for k, v in args.items() if k not in gone}
        if op == "say" and "gesture" in gone and args.get("gesture"):
            out["text"] = f"*{str(args['gesture']).strip('* ')}* {out.get('text', '')}".strip()
        return out

    def _keep_heard(self, events: list[dict[str, Any]]) -> None:
        keep = self.st.get("heard") or []
        have = {h[0] for h in keep}
        asked = self.st.get("unanswered") or {}
        voices = self.st.setdefault("voices", {})
        for e in events or []:
            if e.get("kind") in ("say", "talked") and e.get("actor") not in (None, self.me):
                asked.pop(str(self.names.name(e.get("actor"))).lower(), None)  # they spoke: no longer unanswered
            said = (e.get("payload") or {}).get("text")
            seen = self.st.setdefault("voice_seqs", [])
            if e.get("kind") == "say" and e.get("actor") not in (None, self.me) and isinstance(said, str) and \
                    e.get("seq") not in seen:
                # every line this seat heard, by speaker, for checking what it later says they said
                seen.append(e.get("seq"))
                del seen[:-200]
                who = str(self.names.name(e.get("actor")))
                voices[who] = (voices.pop(who, []) + [said[:400]])[-8:]
                while len(voices) > 40:
                    voices.pop(next(iter(voices)))
            if e.get("kind") != "say" or e.get("seq") in have or not scene.to_me(e, self.me, self.my_name()):
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

    def _voices(self) -> dict[str, list[str]]:
        """What each speaker was heard saying: every line kept, plus the lines to this seat kept before that began."""
        out = {k: list(v) for k, v in (self.st.get("voices") or {}).items()}
        for h in self.st.get("heard") or []:
            if len(h) > 3 and h[2] not in out.get(h[3], []):
                out.setdefault(str(h[3]), []).append(str(h[2]))
        return out

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
        how = "act(verb='follow') while someone is still here, or go there" if "follow" in self.verb_names() \
            else "go there"
        return f"\n\n(harness: {' '.join(dict.fromkeys(out))} If you mean to stay with them, {how}.)"

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
        self.turns += 1
        b = await self.perceive()
        if int(self.cfg.get("keep_wakes", 5)) > 0:
            self._keep_wake(reason, events, st0, b)
        evs = (events or []) + [e for e in (b.get("events") or []) if e.get("seq") not in
                                {x.get("seq") for x in events or []}]
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
        told = bool(heard) or any(scene.to_me(e, self.me, me_name) for e in evs)
        user = prompts.wake(reason, card, agenda, self.shelves["seat"].index(), self.shelves["craft"].index(),
                            recent, heard, str(self.st.get("plan") or ""), told)
        user += self._companion_left(evs)
        if self.imagination is not None:
            user += self.st.pop("imagined_text", "") + self.imagination.wake_text(self.imagination.score(b, me_name))
        speakers = {self.names.name(e.get("actor")) for e in evs if e.get("kind") in ("say", "talked") and
                    e.get("actor") not in (None, self.me)}
        chat = self.st.get("chat") or {}
        long_talk = [p for p in speakers if chat.get(p, 0) >= int(self.cfg.get("chat_turns", 4))]
        if long_talk:
            # two seats that wake on each other's words can trade lines forever with nothing happening
            user += (f"\n\n(harness: you and {', '.join(long_talk)} have traded lines {chat[long_talk[0]]} turns in a "
                     "row with nothing done between. Unless this line changes something, answer in a few words or "
                     "not at all, and sleep on what would move things: a deed, someone arriving, the hour.)")
        if self.turns == 1 and not self.st.get("read_handbook"):
            guide = "mind" if self.role == "mind" else "player"
            user += (f"\n\nThis is your first turn in this seat: read skill('{guide}'), and the world's handbook for "
                     "your seat once (find_tools('handbook'), then handbook() and the pages it lists that fit you); "
                     "memo what matters, then play. Orienting is one beat: a handful of reads, one memo of what you "
                     "learned, then act or sleep.")
            self.st["read_handbook"] = True
        messages: list[dict[str, Any]] = [{"role": "system", "content": sys_text}, {"role": "user", "content": user}]
        turn: dict[str, Any] = {"deeds": 0, "slept": False, "note": "", "calls": []}
        turn["partners"] = [self.names.name(e.get("actor")) for e in evs if scene.to_me(e, self.me, me_name)]
        turn["spoken_to"] = bool(b.get("owed")) or bool(turn["partners"])
        tu = Usage()
        max_steps = int(self.cfg.get("max_steps", 16))
        if reason.startswith("time is up") and not turn["spoken_to"] and \
                not scene.event_lines(evs, self.me, self.names, 1):
            # nothing happened: a look, a deed or a line of its own, and back to sleep
            max_steps = min(max_steps, int(self.cfg.get("quiet_steps", 8)))
        for step in range(max_steps):
            tools = self.registry.tools(self.loaded_tools())
            choice = None
            if step == max_steps - 1:
                # the last step offers only sleep: the turn ends on the model's own note and watch, not a forced one
                tools = [t for t in tools if t["function"]["name"] == "sleep"]
                choice = {"type": "function", "function": {"name": "sleep"}}
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
            calls = msg.get("tool_calls") or []
            text = (msg.get("content") or "").strip() if isinstance(msg.get("content"), str) else ""
            if not calls:
                if not turn.get("prose_nudged") and step < max_steps - 1:
                    # prose outside a tool reaches nobody in the world, and an empty reply leaves no note: say so
                    # once, let the model put it right
                    turn["prose_nudged"] = True
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
            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls,
                             **{k: msg[k] for k in ("reasoning", "reasoning_details", "reasoning_content")
                                if msg.get(k)}})
            for c in calls:
                f = c.get("function") or {}
                name = f.get("name", "")
                args: dict[str, Any] = {}
                try:
                    args = json.loads(f.get("arguments") or "{}") if isinstance(f.get("arguments"), str) \
                        else (f.get("arguments") or {})
                    if not isinstance(args, dict):
                        raise ValueError("arguments are not an object")
                    if choice and name != "sleep":
                        # some models call a tool they were not offered; the last step runs only sleep
                        turn["cut"] = f"{name}({_short(args)})"[:160]
                        out = "(harness: not run; the turn is over)"
                    else:
                        out = await self.call(name, args, turn)
                except (ValueError, TypeError) as e:
                    out = compact({"ok": False, "reason": f"bad arguments for {name}: {e}"})
                except DoorError as e:
                    if e.status in (401, 403):
                        raise
                    out = compact({"ok": False, "reason": f"the server did not answer: {e}; try again or sleep"})
                turn["calls"].append({"tool": name, "args": _short(args if isinstance(args, dict) else {}),
                                      "out": out[:240]})
                # the whole of what the world said, for checking a character's claims later (bh log --full)
                append_jsonl(self.seat.p("calls.jsonl"), {"ts": time.time(), "turn": self.turns, "tool": name,
                                                          "args": args if isinstance(args, dict) else {}, "out": out})
                messages.append({"role": "tool", "tool_call_id": c.get("id", ""), "content": out})
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
        self.st["recent"] = (self.st.get("recent", []) +
                             [f"t={float(b.get('t') or 0):.0f} woke ({reason[:80]}): {note}"])[-8:]
        append_jsonl(self.seat.p("ledger.jsonl"), {"ts": time.time(), "t": b.get("t"), "turn": self.turns,
                                                   "reason": reason, "calls": turn["calls"], "note": note,
                                                   "watch": (self.st.get("watch") or {}).get("until"),
                                                   "usage": tu.to_dict(),
                                                   **({"escalated": turn["escalated"]} if turn.get("escalated") else {}),
                                                   **({"imagined": turn["imagined"]} if turn.get("imagined") else {})})
        _log(self.seat.name, f"turn {self.turns} ({reason[:60]}): {len(turn['calls'])} tool calls; {tu.line()}; "
                             f"{note[:120]}")
        self.save()

    async def run(self) -> None:
        await self.start()
        _log(self.seat.name, f"joined as {(self.bundle.get('you') or {}).get('name')} ({self.role}) "
                             f"with {self.model.spec}")
        reason = ("you just took your seat in the world" if not self.turns else
                  ("your harness restarted and you are back in your seat. The scene, what was said to you "
                   "lately and your last turns' notes are where you left off: pick up from there, no need to read "
                   "your history again"))
        events: list[dict[str, Any]] = []
        while not self.stop:
            try:
                await self.turn(reason, events)
                reason, events = await self.wait_for_wake()
            except DoorError as e:
                if e.status in (401, 403):
                    self.seat.outbox(f"stopped: {e}", "error")
                    _log(self.seat.name, f"stopped: {e}")
                    return
                _log(self.seat.name, f"door trouble: {e}; waiting a minute")
                await asyncio.sleep(60)
                reason, events = "the server was away for a while; look before you act", []
            except asyncio.CancelledError:
                raise
            except Exception as e:  # keep the seat alive; say what broke
                _log(self.seat.name, f"error: {e}\n{traceback.format_exc()}")
                self.seat.outbox(f"harness error: {e}", "error")
                await asyncio.sleep(60)
                reason, events = "the harness recovered from an error; look before you act", []


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
            if isinstance(t.get("when"), str):
                t["when"] = re.sub(r"\b(minute|hour|day)", r"world \1", t["when"])
            t["real_minutes"] = max(1, round(t["in"] / rate / 60))
        out.append(t)
    return out


_TOLD = r"(?:said|says|told (?:me|you|us|him|her|them)|tells (?:me|you|us)|reckons|swore|claims|claimed|mentioned)"
_PLAIN = {"that", "this", "with", "from", "have", "were", "they", "them", "their", "there", "what", "when", "will",
          "would", "about", "your", "just", "been", "some", "someone", "said", "told", "says", "tells", "whole", "also",
          "only", "here", "back", "like", "into", "over", "then", "than", "know", "come", "coming", "going", "sending"}


def _hearsay(text: str, voices: dict[str, list[str]], people: Any = ()) -> tuple[str, str, str] | None:
    """(who, the words given them, the last line heard from them) when `text` says someone said something that
    nothing this seat heard from them carries. Pip told the Reeve "Captain Ruhl said the Guild's sending someone from
    the star to look at our gate"; Ruhl had said nothing of the kind, and corrected him in the hall."""
    names = sorted({str(n) for n in people if n} | set(voices), key=len, reverse=True)
    if not names:
        return None
    alias = {}  # a name or its last word (Ruhl for Captain Ruhl) -> the full name
    for n in names:
        alias.setdefault(n.lower(), n)
        last = n.split()[-1]
        if len(last) > 3:
            alias.setdefault(last.lower(), n)
    pat = "|".join(re.escape(a) for a in sorted(alias, key=len, reverse=True))
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        m = re.search(rf"^\s*(?:and\s+)?(he|she)\s+{_TOLD}\b(.+)", sentence, re.I)
        if m:  # "He said ..." is the last person named before it
            before = text[:text.find(sentence)]
            found = [(before.lower().rfind(a), alias[a]) for a in alias if re.search(rf"\b{re.escape(a)}\b", before,
                                                                                     re.I)]
            if not found:
                continue
            who = max(found)[1]
        else:
            m = re.search(rf"\b({pat})\b[^.!?]{{0,12}}?\b{_TOLD}\b(.+)", sentence, re.I)
            if not m:
                continue
            who = alias[m.group(1).lower()]
        claim = m.group(2).strip(" ,:")
        skip = _PLAIN | {w.lower() for n in names for w in n.split()}
        words = _words(claim, skip)
        if len(words) < 3:
            continue
        lines = voices.get(who) or []
        heard = set()
        for ln in lines:
            heard |= {w[:5] for w in _words(ln, set())}
        if not lines or len({w for w in words if w[:5] in heard}) / len(words) < 0.34:
            return who, claim[:160], (lines[-1][:160] if lines else "")
    return None


def _short(args: dict[str, Any]) -> dict[str, Any]:
    return {k: (v[:160] if isinstance(v, str) else v) for k, v in (args or {}).items()}


async def run_seats(seats: list[Seat]) -> None:
    limits = httpx.Limits(max_connections=max(10, len(seats) * 3), max_keepalive_connections=max(5, len(seats)))
    async with httpx.AsyncClient(limits=limits) as client:
        minds = [Mind(s, client) for s in seats]
        await asyncio.gather(*(m.run() for m in minds))
