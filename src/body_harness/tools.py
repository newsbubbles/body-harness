"""The tools a seat's model sees.

A few are always loaded (CORE). Everything else, the world's other ops, the seat's apps, the harness's extras, is
found with `find_tools` and stays loaded for a while after it is used, the way a deferred tool works in Claude Code.

World ops go through wrappers that fix what minds got wrong most often: a short name where the engine wants the
full one ("Taan" for "Elder Taan"), `target` where an op takes `entity`, a verb's params passed at the top level,
a refusal read as noise. A wrapper never invents a deed: what it sends is what the model asked, made exact."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .loop import Mind

# Ops the harness does itself or that a seat never needs.
HIDDEN_OPS = {"join", "use", "worlds", "wait", "perceive", "answer_ask", "leave"}
# The ops a name in these params must resolve for.
NAME_PARAMS = {"to", "target", "entity", "about", "who"}
# Params minds kept sending under the wrong name, by op.
ALIASES = {"inspect": {"target": "entity", "name": "entity", "id": "entity"},
           "recall": {"who": "about", "target": "about", "entity": "about"},
           "say": {"target": "to", "message": "text", "line": "text"},
           "gesture": {"target": "to"},
           "act": {"entity": "target", "object": "target"},
           "handbook": {"name": "path", "page_name": "path"}}

# Tools minds invented, and what they meant: (tool, how). Corra called gesture(), Ssareth read(); more come from
# each seat's invented.jsonl. A name that is one of the world's verbs goes to act before this list is read.
TOOL_ROUTES = {"gesture": ("say", "gesture"), "emote": ("say", "gesture"), "nod": ("say", "gesture"),
               "speak": ("say", ""), "reply": ("say", ""), "respond": ("say", ""), "talk_to": ("say", ""),
               "tell_npc": ("say", ""), "whisper": ("say", "low"), "shout": ("say", "loud"), "yell": ("say", "loud"),
               "call_out": ("say", "loud"), "walk": ("act", "go"), "move": ("act", "go"), "go_to": ("act", "go"),
               "goto": ("act", "go"), "travel": ("act", "go"), "wait": ("sleep", ""), "end_turn": ("sleep", ""),
               "observe": ("look", ""), "perceive": ("look", ""), "scene": ("look", "")}
_TEXT_KEYS = ("text", "message", "line", "words", "content", "say", "speech")
_PLACE_KEYS = ("target", "place", "to", "destination", "where", "exit")


def route(name: str, args: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """An invented tool name read as the real tool it meant, or None."""
    if name not in TOOL_ROUTES:
        return None
    tool, how = TOOL_ROUTES[name]
    a = dict(args)
    if tool == "say":
        text = next((str(a.pop(k)) for k in _TEXT_KEYS if a.get(k)), "")
        out: dict[str, Any] = {"text": text}
        if how == "gesture":
            g = a.pop("gesture", None) or a.pop("action", None) or a.pop("description", None) or (name if name == "nod" else "")
            out["gesture"] = str(g or "")
            out["text"] = text or f"*{str(g or name).strip('* ')}*"
        elif how:
            out[how] = True
        for k in ("to", "target", "who"):
            if a.get(k):
                out["to"] = a[k]
                break
        return "say", out
    if tool == "act":
        place = next((a.pop(k) for k in _PLACE_KEYS if a.get(k)), None)
        return "act", {"verb": how, "target": place, "params": {k: v for k, v in a.items() if k != "verb"}}
    return tool, {k: v for k, v in a.items() if k in ("until", "minutes", "note", "plan", "part")}


OWNER_KINDS = ("flaw", "vulnerability", "capability", "feature", "fix", "proposal", "quest", "help", "seat",
               "testing", "other")


def fn(name: str, desc: str, props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": desc,
                                             "parameters": {"type": "object", "properties": props,
                                                            "required": required or []}}}


S = {"type": "string"}
CORE: list[dict[str, Any]] = [
    fn("look", "See where you are now: a short scene card (hour, place, who is here, what you hold, exits, goals, "
               "lines owed, what you can do, what happened). part= reads one piece of the last full view instead, "
               "for example part='you.state.dialog', part='present', part='affordances' (refused verbs too, with "
               "reasons).", {"part": S}),
    fn("act", "Do something with your body: one verb from 'You can' on a target, with its params. The world decides "
              "what happens; the answer says what did or why not.",
       {"verb": S, "target": S, "params": {"type": "object"}}, ["verb"]),
    fn("say", "Speak aloud in your place. to= one person here (a short name is fine; the harness finds the full "
              "one). gesture= a sign with the line ('nods'). Words are not deeds: act first, then say what you did.",
       {"text": S, "to": S, "gesture": S, "loud": {"type": "boolean"}, "low": {"type": "boolean"}}, ["text"]),
    fn("remember", "Write a secret note on your character, kept by the world: what you learned, promised or decided. "
                   "The next mind in this body reads it. Only after the world confirmed the thing happened.",
       {"text": S}, ["text"]),
    fn("intend", "Your character's goals, kept by the world and shown every look. New: text + why. Change one: id + "
                 "status (active, done, dropped), why or priority.",
       {"text": S, "why": S, "id": S, "status": S, "priority": {"type": "integer"}}),
    fn("sleep", "End this turn and wait. until= a condition the world checks while you sleep; pick what you would "
                "answer: to_me() (said to you or by your name); heard('', by='Brakka') (anything that person says, "
                "to anyone: use it for whoever you are talking with); said_here() (anyone else here says anything: "
                "a busy scene, a deal, a crowd); arrived('') (anyone walks in); did('attack') or did() (a deed); "
                "heard('water') (a word); me.get('hp', 9) < 3; t >= <a number>. Or them together: \"to_me() or "
                "heard('', by='Brakka') or did('attack')\". Names are the names on your card, any part, any case. "
                "The harness wakes you when it is true, when minutes pass, when your operator writes, or when "
                "something you scheduled comes due. note= one or two lines for your next turn: what you just did, "
                "what you learned that changes the plan, and what you are waiting for. Never a condition that is already true now. "
                "plan= (optional) what you now mean to do, in a line or two; it replaces your old plan, so write it "
                "whenever someone corrects you or your aim changes.",
       {"until": S, "minutes": {"type": "number"}, "note": S, "plan": S}, ["note"]),
    fn("find_tools", "Search for more tools and load them: the world's other ops (inspect, recall, find, history, "
                     "away, gesture, handbook, guide, timers...), your apps, skills, and the harness's extras "
                     "(schedule, run_python, write_file). query= words for what you need. The tools it finds are "
                     "loaded for your next calls.", {"query": S}, ["query"]),
    fn("tell", "Speak outside the fiction. to='owner': the world's owner, the only way to reach them: kind is flaw "
               "or vulnerability (a hole in the rules; keep it secret and leave it alone), capability or feature (a "
               "verb the world lacks), fix (something broken), proposal (a change, with credit), quest (you need a "
               "lead), help (you are stuck), seat, testing or other. to='operator': the human who runs this "
               "harness (a question, a report, a problem). Never put either into the world as speech.",
       {"to": {"type": "string", "enum": ["owner", "operator"]},
        "kind": {"type": "string", "enum": list(OWNER_KINDS)},
        "what": S, "why": S, "severity": {"type": "string", "enum": ["low", "medium", "high"]}, "credit": S},
       ["to", "what"]),
    fn("memo", "Your own notes outside the world (markdown files). shelf='seat' is this character's, 'craft' is "
               "shared with every seat you run (lessons about playing well, never a character's secrets). "
               "op: list, read, write (name, text, hook: one line for the index), append, search (query).",
       {"op": {"type": "string", "enum": ["list", "read", "write", "append", "search"]}, "name": S, "text": S,
        "hook": S, "shelf": {"type": "string", "enum": ["seat", "craft"]}, "query": S}, ["op"]),
    fn("skill", "Read a skill from the index in your instructions (how to do something well). part= a file inside "
                "it.", {"name": S, "part": S}, ["name"]),
]
CORE_NAMES = {t["function"]["name"] for t in CORE}

# Anthropic's think tool: a place to reason between tool results, for models that do not reason on their own. It
# reaches nobody and changes nothing. Off unless a seat's config says think_tool=true (measure it with bh calibrate).
THINK = fn("think", "Think before you act, when a tool result or a line changes things: check what you believe "
                    "against what the world just said, what was asked of you, and what you mean to do. Nobody sees "
                    "it and nothing in the world changes. Do not use it to stall: act, say or sleep after.",
           {"thought": S}, ["thought"])

# Imagination (imagine.py): a private embody world of the seat's own, offered when a seat's config says imagine=true
# and the embody-engine extra is installed. The harness scores what it imagined against what really happened.
IMAGINE = fn("imagine", "Try a plan in your imagination before you do it: a private world with the rules as you "
                        "believe them, played many times with fresh dice. Nothing real happens and nobody sees it. "
                        "rules= {verb: {check: 'return ctx.actor.state.hp > 4', apply: 'if ctx.rand.roll(\"1d20\") "
                        "+ 3 >= ctx.target.state.ac: ctx.target.state.hp -= ctx.rand.roll(\"1d6\")'}}, named after "
                        "your real verbs: check is when it can happen, apply what happens (a Python subset, no "
                        "imports; dice from ctx.rand.roll). setup= {name: "
                        "{hp, ac, atk, gold...}}: what you believe of someone. You are 'me', from your real card. "
                        "Both are kept for next time. plan= steps [{actor, verb, target}], played in order `rounds` "
                        "times, until a check says no. This world is yours: anyone in it does what you say, so a "
                        "step for someone else is your guess of what they would do. Unsure, give odds, drawn afresh "
                        "each trial: {actor: 'Oren', verb: {'guard': 3, 'strike': 1}}; or, when both choose at "
                        "once, a choice inside your own step: {verb: 'strike', target: 'Oren', odds: {'his': "
                        "{'guard': 3, 'throw': 1}}}, which your rule reads as ctx.params.his. When you learn how "
                        "someone chooses, keep it as their mind, kept for next time like a rule: minds= {'Oren': "
                        "{choose: 'if ctx.params.theirs[-2:] == [\"strike\", \"strike\"]: return \"guard\"\\nreturn "
                        "{\"guard\": 2, \"throw\": 1}', as: 'his'}}: it returns a move or odds; ctx.actor is them, "
                        "ctx.target who they face, ctx.params.theirs what that one did lately (your real deeds "
                        "first), ctx.params.mine what they did. A step for them with no verb is then theirs to "
                        "choose, and your rules' ctx.params.his comes from it when you give none. It answers how "
                        "things came out over the trials. When you then "
                        "really do the plan's first step, the harness compares what happens with what you imagined "
                        "and tells you how surprised to be. With nothing given, it shows your rules and beliefs (and "
                        "a starter).",
             {"rules": {"type": "object"}, "setup": {"type": "object"}, "minds": {"type": "object"},
              "plan": {"type": "array", "items": {"type": "object"}}, "rounds": {"type": "integer"},
              "trials": {"type": "integer"}})
REFLECT = fn("reflect", "Look back on what you imagined against what really happened. With no id: your biggest "
                        "surprises, the numbers you have really seen per kind of plan, how well your imagination did, "
                        "and your lessons. With id, verdict and lesson (one line, with the numbers): belief (someone's "
                        "numbers were off: check this first), rule (the same thing keeps happening that no numbers "
                        "in your rule could produce), or luck (one result your dice allow). Change the belief or rule "
                        "with imagine() too; the lesson is shown on your next imaginings.",
             {"id": S, "verdict": {"type": "string", "enum": ["belief", "rule", "luck"]}, "lesson": S})
RELIVE = fn("relive", "Live a past moment again in your imagination: the numbers you saw then and what you really "
                      "did, played through the rules, beliefs and minds you have now (a mind you keep for someone "
                      "chooses for them in place of the guess you made then). With no id, every moment you lived "
                      "is re-lived: how many your imagination now sees coming, against when you lived them. With "
                      "id (a plan you imagined and then did), after=N of your real steps and instead=[{actor, verb, "
                      "target}], imagine what would have happened had you done something else from there.",
            {"id": S, "after": {"type": "integer"}, "instead": {"type": "array", "items": {"type": "object"}}})

PAST = fn("past", "Search what you really did and what came of it: every deed (act, say) you did, with its result, "
                  "lined up so you can see patterns across many moments. verb= 'strike' or 'strike|throw'. where= "
                  "fields of the result: {'they': 'guard', 'took': '>0'} (=, !=, >, <, >=, <= for numbers; 'a|b' "
                  "for either). match= a regex over the whole moment. seq= a run of your verbs in a row, ['strike', "
                  "'strike']: matches the deed that ends it. count= a field to tally over every match, 'they'; "
                  "'next.they' tallies the deed after each match. before=/after= deeds of context around each match "
                  "(up to 5). last= only your last N deeds. limit= how many matches to show (12, up to 40). With "
                  "nothing given: your last 12 deeds. Example: what does Oren do after I strike twice? "
                  "past(seq=['strike', 'strike'], count='next.they').",
          {"verb": S, "where": {"type": "object"}, "match": S, "seq": {"type": "array", "items": S},
           "count": S, "before": {"type": "integer"}, "after": {"type": "integer"}, "last": {"type": "integer"},
           "limit": {"type": "integer"}})

EXTRAS: dict[str, dict[str, Any]] = {
    "schedule": fn("schedule", "Ask to be woken later for something: at_t= a world time in seconds, or "
                               "in_minutes= real minutes from now; what= what to do then. op='list' shows what is "
                               "pending, op='drop' with id removes one.",
                   {"op": {"type": "string", "enum": ["add", "list", "drop"]}, "at_t": {"type": "number"},
                    "in_minutes": {"type": "number"}, "what": S, "id": S}),
    "run_python": fn("run_python", "Run Python in your own sandbox (your workspace folder; no network, no other "
                                   "processes). For your own sums, tallies and plans. workspace/scene.json is the "
                                   "last full view of the world. Print what you want back.",
                     {"code": S, "timeout": {"type": "number"}}, ["code"]),
    "write_file": fn("write_file", "Write a file under workspace/ or apps/. An app is apps/<name>.py with TOOL = "
                                   "{'description': ..., 'parameters': {JSON schema}} and def run(args): returning "
                                   "text or JSON; it becomes a tool you can find. skill('apps') says how.",
                     {"path": S, "text": S}, ["path", "text"]),
    "agenda": fn("agenda", "What is on your plate: lines owed, your operator's messages, the owner's answers, your "
                           "goals, what you scheduled, and your last turns.", {}),
}


class Registry:
    def __init__(self, mind: "Mind"):
        self.m = mind
        self.world: dict[str, dict[str, Any]] = {}  # op -> {summary, description, schema}

    async def load_world_ops(self) -> None:
        """The world ops this seat may call: its mind's grant's, then each hand's that the mind lacks."""
        await self._load_ops(self.m.door, self.m.role, set(self.m.ops or []), "")
        for h, hand in getattr(self.m, "hands", {}).items():
            if hand["ops"]:
                await self._load_ops(hand["door"], hand["role"], hand["ops"], h)

    async def _load_ops(self, door: Any, role: str, allowed: set[str], hand: str) -> None:
        try:
            spec = await door.get(f"/openapi.json?role={role}")
        except Exception:
            spec = {}
        for path, item in (spec.get("paths") or {}).items():
            op = (item.get("post") or {})
            name = op.get("operationId") or path.rsplit("/", 1)[-1]
            if name in HIDDEN_OPS or name in CORE_NAMES or (allowed and name not in allowed):
                continue
            if hand and name in self.world:
                self.world[name]["hands"].append(hand)
                continue
            schema = (((op.get("requestBody") or {}).get("content") or {}).get("application/json") or {}).get(
                "schema") or {"type": "object", "properties": {}}
            schema = {k: v for k, v in schema.items() if k in ("type", "properties", "required")}
            schema.setdefault("type", "object")
            schema.setdefault("properties", {})
            desc = (op.get("description") or op.get("summary") or "").split("**Layer:**")[0].strip()
            if hand:
                desc = f"(through your builder hand {hand}, which reaches only its own part of the world) " + desc
            self.world[name] = {"summary": op.get("summary") or desc[:160], "description": desc[:1500],
                                "schema": schema, "layer": op.get("x-layer", ""), "hands": [hand] if hand else []}

    def catalog(self) -> list[tuple[str, str, str]]:
        """(name, kind, one line) for every findable thing."""
        out = [(n, "world op", w["summary"]) for n, w in self.world.items()]
        out += [(n, "harness", t["function"]["description"][:160]) for n, t in EXTRAS.items()]
        out += [(n, "app", s.get("description", "")[:160]) for n, s in self.m.apps().items()]
        out += [(f"skill:{n}", "skill", d) for n, d in self.m.skills.desc.items()]
        return out

    def find(self, query: str) -> dict[str, Any]:
        words = [w for w in query.lower().replace(",", " ").split() if len(w) > 1]
        scored = []
        for name, kind, line in self.catalog():
            hay = f"{name} {line}".lower()
            score = sum(3 if w in name.lower() else 1 for w in words if w in hay)
            if query.strip().lower() == name.lower():
                score += 10
            if score:
                scored.append((score, name, kind, line))
        scored.sort(key=lambda x: -x[0])
        top = scored[:6]
        loaded = [n for _, n, k, _ in top if k != "skill"]
        self.m.load_tools(loaded)
        return {"loaded": loaded,
                "found": [{"name": n, "kind": k, "what": ln} for _, n, k, ln in top],
                "hint": "loaded tools are callable now; a skill is read with skill(name)" if top else
                        "nothing matched; try other words, or look at 'You can' in look() for verbs"}

    def schema(self, name: str) -> dict[str, Any] | None:
        if name in EXTRAS:
            return EXTRAS[name]
        if name in self.world:
            w = self.world[name]
            return fn(name, w["description"][:900], w["schema"]["properties"], w["schema"].get("required"))
        a = self.m.apps().get(name)
        if a:
            p = a.get("parameters") or {"type": "object", "properties": {}}
            return fn(name, a.get("description", "")[:900], p.get("properties", {}), p.get("required"))
        return None

    def tools(self, loaded: list[str]) -> list[dict[str, Any]]:
        core = CORE + [THINK] if self.m.cfg.get("think_tool") else CORE
        if self.m.imagination is not None:
            core = core + [IMAGINE, REFLECT, RELIVE]
        if self.m.cfg.get("past"):
            core = core + [PAST]
        return core + [s for s in (self.schema(n) for n in loaded) if s]


def fix_args(op: str, args: dict[str, Any], mind: "Mind") -> tuple[dict[str, Any], list[str]]:
    """Rename what minds send under the wrong name and resolve short names. Returns the args and what was changed."""
    notes: list[str] = []
    args = dict(args)
    for wrong, right in ALIASES.get(op, {}).items():
        if wrong in args and right not in args:
            args[right] = args.pop(wrong)
            notes.append(f"{wrong}= read as {right}=")
    if op == "act":
        sch = mind.registry.world.get("act", {}).get("schema", {}).get("properties", {})
        stray = {k: v for k, v in args.items() if k not in ("verb", "target", "params") and k not in sch}
        if stray:
            args["params"] = {**(args.get("params") or {}), **stray}
            for k in stray:
                args.pop(k)
            notes.append(f"{', '.join(stray)} moved into params")
        if not args.get("target") and isinstance(args.get("params"), dict):
            # act(verb='go', params={'go': 'Harbour Street'}): a param whose value names something here is the target
            here = {n.lower(): n for n in mind.present_names()}
            for k, v in list(args["params"].items()):
                if isinstance(v, str) and v.strip().lower() in here:
                    args["target"] = here[v.strip().lower()]
                    args["params"].pop(k)
                    notes.append(f"params.{k}={v!r} read as target=")
                    break
    for k in NAME_PARAMS & set(args):
        v = args[k]
        if not isinstance(v, str):
            continue
        full, cands = mind.names.resolve(v, mind.present_names())
        if full and full != v:
            args[k] = full
            notes.append(f"{k}={v!r} is {full!r}")
        elif not full and cands:
            notes.append(f"{k}={v!r} could be: {', '.join(cands)}")
    return args, notes


def compact(result: Any, limit: int = 5000) -> str:
    """A world answer for the model: refusals as reason, hint and what would work; big answers cut with a note."""
    if isinstance(result, dict) and result.get("ok") is False:
        r = {"refused": result.get("reason")}
        if result.get("hint"):
            r["hint"] = result["hint"]
        if result.get("alternatives"):
            r["instead"] = result["alternatives"]
        for k in ("present", "names", "how", "kinds"):
            if k in result:
                r[k] = result[k]
        return json.dumps(r, default=str)[:limit]
    if isinstance(result, dict):
        result = {k: v for k, v in result.items() if k not in ("cursor", "wall")}
        if isinstance(result.get("events"), list) and len(result["events"]) > 12:
            result["events"] = result["events"][-12:]
            result["events_note"] = "older events cut; look() shows the scene"
    s = result if isinstance(result, str) else json.dumps(result, default=str)
    if len(s) > limit:
        s = s[:limit] + f"... (cut at {limit} of {len(s)} characters; ask for less, or page it)"
    return s
