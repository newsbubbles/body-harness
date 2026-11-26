"""A plain agent, the baseline the harness is measured against: the same model and the same world, with the engine's
ops as its tools (raw JSON in, raw JSON out), the world's briefing as its system prompt, and one conversation kept
across the case's turns, as a chat harness keeps it. No scene card, no nudges, no notebook, no name fixing.

The suite runs it in place of the harness with --variant P:plain=true. It writes the same logs a seat writes
(calls.jsonl, ledger.jsonl, usage.jsonl, state.json), so every case's check reads it unchanged.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from body_harness.providers import Model
from body_harness.seat import Seat, append_jsonl
from body_harness.tools import fn, parse_args
from body_harness.usage import Usage, append as usage_append

# not a character's: joining, leaving, the seat and owner plumbing
SKIP = {"join", "leave", "worlds", "use", "answer_ask", "claim_seat", "principals", "seats", "health"}


class PlainMind:
    def __init__(self, seat: Seat, client: httpx.AsyncClient):
        self.seat, self.client = seat, client
        self.cfg = seat.config()
        self.model = Model.from_config(self.cfg.get("model", ""), {**self.cfg, "session": f"plain-{time.time():.0f}"})
        self.st: dict[str, Any] = seat.state()
        self.turns = int(self.st.get("turns", 0))
        self.door: Any = None
        self.messages: list[dict[str, Any]] = []
        self.tools: list[dict[str, Any]] = []

    async def start(self) -> None:
        j = await self.door.op("join")
        spec = await self.door.get("/openapi.json?role=mind")
        for path, item in (spec.get("paths") or {}).items():
            op = item.get("post") or {}
            name = op.get("operationId") or path.rsplit("/", 1)[-1]
            if name in SKIP:
                continue
            schema = (((op.get("requestBody") or {}).get("content") or {}).get("application/json") or {}).get(
                "schema") or {}
            self.tools.append(fn(name, (op.get("description") or op.get("summary") or "")[:1500],
                                 schema.get("properties") or {}, schema.get("required")))
        b = (j or {}).get("briefing") or {}
        you = ((j or {}).get("you") or {}).get("name") or self.seat.name
        notes = [str(x) for x in self.st.get("recent") or []] + ([f"plan: {self.st['plan']}"] if self.st.get("plan")
                                                                  else [])
        self.messages = [{"role": "system", "content": "\n\n".join(x for x in (
            f"You are {you}, a character in {b.get('world', 'a world')}, a world on embody. The world's engine keeps "
            "the truth; you are this one body's mind. Play your character through the tools: perceive to see, act "
            "to do, say to speak. When you are done for now, call wait.",
            ("The world: " + str(b["text"])) if b.get("text") else "",
            ("Your notes from your earlier turns:\n" + "\n".join(f"- {n}" for n in notes)) if notes else "") if x)}]

    async def turn(self, reason: str, events: list[dict[str, Any]]) -> None:
        self.turns += 1
        b = await self.door.op("perceive", events=40)
        self.messages.append({"role": "user", "content": f"{reason}\n\nperceive():\n"
                                                         + json.dumps(b, default=str)[:20000]})
        tu, note = Usage(), ""
        for _ in range(int(self.cfg.get("max_steps", 16))):
            msg, u = await self.model.chat(self.client, self.messages, self.tools)
            tu.add(u)
            usage_append(self.seat.p("usage.jsonl"), self.seat.name, self.model.spec, u, reason[:80])
            calls = msg.get("tool_calls") or []
            note = (msg.get("content") or "").strip() if isinstance(msg.get("content"), str) else note
            self.messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls,
                                  **{k: msg[k] for k in ("reasoning", "reasoning_details", "reasoning_content")
                                     if msg.get(k)}} if calls else {"role": "assistant", "content": msg.get("content") or ""})
            if not calls:
                break
            done = False
            for c in calls:
                f = c.get("function") or {}
                name = f.get("name", "")
                args, _, err = parse_args(f.get("arguments"))
                if err or args is None:
                    out: Any = {"ok": False, "reason": err}
                elif name == "wait":
                    out, done = {"ok": True, "waiting": args.get("until") or ""}, True
                    note = note or str(args.get("until") or "")
                else:
                    out = await self.door.op(name, **args)
                text = json.dumps(out, default=str)[:12000]
                append_jsonl(self.seat.p("calls.jsonl"), {"ts": time.time(), "turn": self.turns, "tool": name,
                                                          "args": args or {}, "out": text})
                self.messages.append({"role": "tool", "tool_call_id": c.get("id", ""), "content": text})
            if done:
                break
        append_jsonl(self.seat.p("ledger.jsonl"), {"ts": time.time(), "turn": self.turns, "reason": reason,
                                                   "note": note[:400], "usage": tu.to_dict()})

    def save(self) -> None:
        self.st["turns"] = self.turns
        self.seat.save_state(self.st)
