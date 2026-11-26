# Telemetry

A seat can report counts of what it does to a telemetry server its operator runs (body-telemetry), for dashboards
of spend, context, tool use and behaviour across seats. It is off unless the seat or home config sets it:

```json
"telemetry": {"url": "http://127.0.0.1:8899", "key_env": "BODY_TELEMETRY_KEY", "detail": "counts",
              "machine": "", "run": ""}
```

Every event goes to the seat's `telemetry.jsonl` first and is posted in batches every 20 seconds. A server that is
down or slow never slows a turn: the spool keeps what was not taken (the newest 50,000 events) and posts it later.

## What leaves the machine

| detail | sends |
|---|---|
| `counts` (default) | numbers; the names of tools, verbs, models, the world and the character; ok or refused, and the world's refusal code |
| `lines` | also the wake reason, the turn note and a refusal's reason, each cut to 120 characters |
| `full` | the same, uncut. Players' words are other people's words: choose this only for a world you own |

Never sent at any level: tokens, door files, invites, keys, the brief, the model's messages, tool arguments other
than an act's verb.

## Wire format

`POST <url>/v1/events` with `{"events": [...]}`, at most 500 a batch, and `Authorization: Bearer <key>` when the
environment variable named by `key_env` is set. A 2xx answer means the whole batch was taken. A 4xx other than 401,
403, 408 or 429 means the server will never take it: the events are posted one at a time and the refused ones are
dropped. Anything else, and the batch is posted again later. Each event has a `uid`, so one posted twice is stored
once.

Every event carries these labels:

| field | what |
|---|---|
| `ts` | Unix seconds |
| `uid`, `kind` | a unique id; `seat`, `setup`, `model_call`, `tool_call`, `turn`, `tell`, `invented` |
| `harness`, `version` | `body-harness` and its version |
| `settings` | a 12-character hash of the seat's settings (telemetry, keys and the operator's own words left out) |
| `switches` | the main settings as booleans: menu, menu_rank, menu_preopen, menu_lists, reflexes, imagine, past, think_tool, escalate |
| `machine`, `run` | labels the operator sets (empty by default) |
| `seat`, `world`, `character`, `role` | the seat's name, the world's name, the character's name, mind or player |

And by kind:

| kind | fields |
|---|---|
| `seat` | `event`: start or restart (with `turns`, `model`), server away, capped, stop (`why`, `status`), error (the error's type) |
| `setup` | `setup`: the seat's settings that are bools, numbers or model names (model, judge, escalate); never prompts, providers, files or anything named like a key or token |
| `model_call` | `model`, `use` (turn or judge), `turn`, `step`, `rung` (escalations so far this turn), `rules` (judge), and the usage numbers: `calls`, `input_tokens`, `cached_tokens`, `cache_write_tokens`, `output_tokens`, `reasoning_tokens`, `cost`, `seconds`; on turn calls `ctx`, the characters of the context by part: system, tools, wake, history, tool_results |
| `tool_call` | `turn`, `tool`, `layer` (verb, engine, menu, app, harness, invented), `verb` (for act), `ok`, `code`, `secs`, `out_chars`; `refusal` at lines or full |
| `turn` | `turn`, `model`, `steps`, `tool_calls`, `deeds`, `says`, `reads`, `opens` (menu), `nudges`, `nudge_kinds` (the word before each nudge's colon, e.g. stuck, last_step), `escalated` (models), `ended` (slept, out of steps, other), `wall`, the usage numbers; `reason` and `note` at lines or full |
| `tell` | `to` (operator or owner), `about` (its kind), `out_chars` |
| `invented` | `model`, `tool` (the made-up name), `read_as` |

A server should keep fields it does not know, so a newer harness never breaks an older server.
