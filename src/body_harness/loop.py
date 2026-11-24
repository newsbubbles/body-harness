"""A mind: one seat, one body, the loop that keeps it alive.

    join -> turn -> sleep -> (the harness waits, no model) -> wake -> turn -> ...

A turn is a fresh, short conversation: the stable system prompt, then one wake message (why, the scene, the agenda,
the last few turns' notes). The model calls tools until it calls sleep. Then the harness waits on the world with the
model's condition, in pieces under the server's cap, and wakes the model only for something: the condition, a
deadline, the operator, a scheduled item, an owed line, a rules change. The seat is kept, lapses are rejoined, the
standing orders are set again after every deed, and the door is retried with backoff when the server is away."""

from __future__ import annotations

import asyncio
import json
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

import httpx

from . import prompts, sandbox, scene
from .door import Door, DoorError
from .memory import Shelf, memo
from .providers import Model, ModelError
from .seat import Seat, append_jsonl, read_jsonl
from .skills import Skills
from .tools import EXTRAS, Registry, compact, fix_args
from .usage import Usage, append as usage_append

DEEDS = {"act", "say", "gesture", "step", "cross", "act_many"}
KEEP_LOADED_TURNS = 5
MAX_LOADED = 8


def _log(seat: str, msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {seat}: {msg}", flush=True)


class Mind:
    def __init__(self, seat: Seat, client: httpx.AsyncClient):
        self.seat = seat
        self.client = client
        self.cfg = seat.config()
        self.door = Door.load(seat.p("door.json"), client)
        self.door.wait_cap = float(self.cfg.get("wait_cap", self.door.wait_cap))
        self.model = Model.from_config(self.cfg.get("model", ""), {**self.cfg, "session": self.cfg.get("session") or
                                       f"bh-{seat.name}-{uuid.uuid4().hex[:6]}"})
        self.st: dict[str, Any] = seat.state()
        self.names = scene.Names(self.st.get("names"))
        self.skills = Skills(seat.home / "skills", seat.p("skills"))
        self.shelves = {"seat": Shelf(seat.p("memory")), "craft": Shelf(seat.home / "craft")}
        self.registry = Registry(self)
        self.bundle: dict[str, Any] = {}
        self.briefing: dict[str, Any] = self.st.get("briefing", {})
        self.role = self.briefing.get("role", self.cfg.get("role", "mind"))
        self.ops: list[str] = []
        self.me = self.st.get("me", "")
        self.usage = Usage()
        self.turns = int(self.st.get("turns", 0))
        self.day_seconds = float(self.cfg.get("day_seconds", scene.DAY))
        self.stop = False

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

    def present_names(self) -> list[str]:
        b = self.bundle
        names = [x.get("name") for x in (b.get("present") or []) if isinstance(x, dict)]
        names += [x.get("name") for x in (b.get("holding") or []) if isinstance(x, dict)]
        names += [x.get("name") for x in (b.get("exits") or []) if isinstance(x, dict)]
        return [n for n in names if isinstance(n, str)]

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
        return j

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
        await self.registry.load_world_ops()
        try:
            hb = await self.door.op("handbook")
            self.st["handbook"] = [{"path": "SKILL.md", "description": "the handbook's own index"}] + \
                                  [p for p in (hb.get("pages") or []) if isinstance(p, dict)]
        except DoorError:
            pass
        self.save()

    # ---------- tools ----------

    async def call(self, name: str, args: dict[str, Any], turn: dict[str, Any]) -> str:
        if name == "look":
            if args.get("part"):
                if args["part"].strip().lower() in ("affordances", "you can", "verbs"):
                    return "\n".join(scene.affordance_lines(self.bundle.get("affordances") or [], True))
                return compact(scene.part(self.bundle, args["part"]), 8000)
            b = await self.perceive()
            return scene.card(b, self.names, self.me, self.day_seconds)
        if name == "sleep":
            return await self._sleep(args, turn)
        if name == "find_tools":
            return compact(self.registry.find(str(args.get("query", ""))))
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
            return compact({"ok": False, "reason": f"no tool {name}", "hint": "find_tools(query) finds and loads "
                                                                                "tools; verbs go through act(verb, ...)"})
        fixed, notes = fix_args(name, args, self)
        if name == "away":
            self.st["away"] = fixed
        r = await self.door.op(name, **fixed)
        if isinstance(r, dict) and r.get("ok") is False and "join" in str(r.get("reason", "")).lower() \
                and "seat" in str(r.get("reason", "")).lower():
            await self.join()
            r = await self.door.op(name, **fixed)
        ok = not (isinstance(r, dict) and r.get("ok") is False)
        if ok and name in DEEDS:
            turn["deeds"] += 1
        if name in self.registry.world:
            self.load_tools([name])
        if isinstance(r, dict) and isinstance(r.get("events"), list):
            lines = scene.event_lines(r["events"], self.me, self.names, 12)
            r = {k: v for k, v in r.items() if k not in ("events", "cursor")}
            if lines:
                r["what happened"] = lines
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

    async def _sleep(self, a: dict[str, Any], turn: dict[str, Any]) -> str:
        until = (a.get("until") or "").strip() or self.cfg.get("default_until", "to_me()")
        minutes = float(a.get("minutes") or self.cfg.get("default_minutes", 60))
        minutes = max(1.0, min(minutes, float(self.cfg.get("max_minutes", 480))))
        if turn["deeds"] and self.st.get("away"):
            # a deed ends the standing orders; set them again before sleeping
            await self.door.op("away", **self.st["away"])
        # arm it once, briefly: the engine checks the grammar, and anything that happened while we decided comes now
        w = await self.door.wait(until, 1)
        if isinstance(w, dict) and w.get("ok") is False:
            return compact({"refused": w.get("reason"), "hint": w.get("hint"),
                            "fix": "write the condition again (skill('waiting') has the grammar), then sleep"})
        if isinstance(w, dict) and w.get("fired"):
            if w.get("already"):
                return ("Not asleep: that condition is already true now, so it would wake you at once. Pick one "
                        "that is false now (a change you are waiting for), then sleep.")
            lines = scene.event_lines(w.get("events") or [], self.me, self.names)
            return ("Not asleep: while you were deciding, it happened:\n" + "\n".join(lines) +
                    "\nAnswer it if it needs you, then sleep again.")
        self.st["watch"] = {"until": until, "deadline": time.time() + minutes * 60, "armed": time.time()}
        note = str(a.get("note") or "").strip()
        turn["note"] = note
        turn["slept"] = True
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
            step = min(self.door.wait_cap, watch["deadline"] - now, (due_real - now) if due_real else 1e9)
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

    async def turn(self, reason: str, events: list[dict[str, Any]]) -> None:
        self.turns += 1
        b = await self.perceive()
        evs = (events or []) + [e for e in (b.get("events") or []) if e.get("seq") not in
                                {x.get("seq") for x in events or []}]
        card = scene.card(b, self.names, self.me, self.day_seconds, events=evs)
        agenda = self.agenda()
        self.st["inbox_seen"] = time.time()
        recent = self.st.get("recent", [])[-6:]
        sys_text = prompts.system(self.seat.name, self.briefing, b.get("you") or {}, self.skills.index(),
                                  self.st.get("handbook") or [], self.cfg.get("extra_prompt", ""))
        user = prompts.wake(reason, card, agenda, self.shelves["seat"].index(), self.shelves["craft"].index(),
                            recent)
        if self.turns == 1 and not self.st.get("read_handbook"):
            guide = "mind" if self.role == "mind" else "player"
            user += (f"\n\nThis is your first turn in this seat: read skill('{guide}'), and the world's handbook for "
                     "your seat once (find_tools('handbook'), then handbook() and the pages it lists that fit you); "
                     "memo what matters, then play.")
            self.st["read_handbook"] = True
        messages: list[dict[str, Any]] = [{"role": "system", "content": sys_text}, {"role": "user", "content": user}]
        turn: dict[str, Any] = {"deeds": 0, "slept": False, "note": "", "calls": []}
        tu = Usage()
        max_steps = int(self.cfg.get("max_steps", 16))
        for step in range(max_steps):
            tools = self.registry.tools(self.loaded_tools())
            try:
                msg, u = await self.model.chat(self.client, messages, tools)
            except ModelError as e:
                _log(self.seat.name, f"model error: {e}")
                self.seat.outbox(f"model error: {e}", "error")
                turn["note"] = turn["note"] or f"(the model failed: {str(e)[:120]})"
                break
            tu.add(u)
            usage_append(self.seat.p("usage.jsonl"), self.seat.name, self.model.spec, u, reason[:80])
            calls = msg.get("tool_calls") or []
            text = (msg.get("content") or "").strip() if isinstance(msg.get("content"), str) else ""
            if not calls:
                if text:
                    turn["note"] = text[:400]
                if step == 0 or not turn["slept"]:
                    # the model answered in prose instead of sleeping: keep the last watch and sleep
                    await self._sleep({"note": text[:400]} if not self.st.get("watch") else
                                      {"until": self.st["watch"]["until"], "note": text[:400]}, turn)
                break
            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
            for c in calls:
                f = c.get("function") or {}
                name = f.get("name", "")
                args: dict[str, Any] = {}
                try:
                    args = json.loads(f.get("arguments") or "{}") if isinstance(f.get("arguments"), str) \
                        else (f.get("arguments") or {})
                    if not isinstance(args, dict):
                        raise ValueError("arguments are not an object")
                    out = await self.call(name, args, turn)
                except (ValueError, TypeError) as e:
                    out = compact({"ok": False, "reason": f"bad arguments for {name}: {e}"})
                except DoorError as e:
                    if e.status in (401, 403):
                        raise
                    out = compact({"ok": False, "reason": f"the server did not answer: {e}; try again or sleep"})
                turn["calls"].append({"tool": name, "args": _short(args if isinstance(args, dict) else {}),
                                      "out": out[:240]})
                messages.append({"role": "tool", "tool_call_id": c.get("id", ""), "content": out})
            if turn["slept"]:
                break
        if not turn["slept"]:
            await self._sleep({"until": (self.st.get("watch") or {}).get("until"),
                               "note": turn["note"] or "(the turn ran out of steps)"}, turn)
        self.usage.add(tu)
        note = turn["note"] or "(no note)"
        self.st["recent"] = (self.st.get("recent", []) +
                             [f"t={float(b.get('t') or 0):.0f} woke ({reason[:80]}): {note}"])[-8:]
        append_jsonl(self.seat.p("ledger.jsonl"), {"ts": time.time(), "t": b.get("t"), "turn": self.turns,
                                                   "reason": reason, "calls": turn["calls"], "note": note,
                                                   "watch": (self.st.get("watch") or {}).get("until"),
                                                   "usage": tu.to_dict()})
        _log(self.seat.name, f"turn {self.turns} ({reason[:60]}): {len(turn['calls'])} tool calls; {tu.line()}; "
                             f"{note[:120]}")
        self.save()

    async def run(self) -> None:
        await self.start()
        _log(self.seat.name, f"joined as {(self.bundle.get('you') or {}).get('name')} ({self.role}) "
                             f"with {self.model.spec}")
        reason, events = "you just took your seat in the world", []
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


def _short(args: dict[str, Any]) -> dict[str, Any]:
    return {k: (v[:160] if isinstance(v, str) else v) for k, v in (args or {}).items()}


async def run_seats(seats: list[Seat]) -> None:
    limits = httpx.Limits(max_connections=max(10, len(seats) * 3), max_keepalive_connections=max(5, len(seats)))
    async with httpx.AsyncClient(limits=limits) as client:
        minds = [Mind(s, client) for s in seats]
        await asyncio.gather(*(m.run() for m in minds))
