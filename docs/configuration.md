# Configuration

Seats live in `$BH_HOME` (default `~/.body-harness`), never in this repository: each holds a token and a
character's private notes. `$BH_HOME/config.json` sets defaults for every seat; `seats/<name>/config.json` overrides.
`bh config <seat> key=value ...` shows or sets a seat's settings (JSON values work: `escalate='["p:m"]'`).

| key | default | what |
|---|---|---|
| `model` | | `provider:model`. Providers: openrouter, ollama, vllm, lmstudio, llamacpp, openai, deepseek, groq, or your own under `providers` |
| `providers` | | `{"mine": {"base": "http://host/v1", "key": "MINE_API_KEY", "local": true}}` |
| `env_files` | | `.env` files to read a provider key from (it is never printed) |
| `max_tokens` | 1200 | per model reply |
| `temperature`, `reasoning` | | passed through (`reasoning` is OpenRouter's effort: low, medium, high) |
| `extra` | | merged into every request body; on OpenRouter, `{"provider": {"order": ["<endpoint>"], "require_parameters": true}}` keeps a seat on the endpoints you chose |
| `cache` | auto | breakpoints on OpenRouter models that need them; `on` or `off` to force |
| `prices` | | `{"provider:model": [in, cached_in, out]}` dollars per million tokens, for providers that do not report cost |
| `max_room` | 4000 | how far `max_tokens` grows when a thinking model's reply is cut off before it says anything |
| `escalate`, `escalate_after` | , 2 | `provider:model` list, in order; after that many calls in one turn that the model got wrong (bad arguments, made-up tools, a watch that does not parse, an empty reply, a model error), the rest of the turn goes to the next one on the list, with the same messages. The next turn starts on `model` again. A world refusing by its rules never escalates |
| `escalate_reads` | 8 | reads (look, find_tools, inspect, history...) in one turn with no deed before it escalates: floundering |
| `max_steps` | 16 | tool rounds in one turn |
| `quiet_steps` | 8 | tool rounds when a watch timed out and nothing happened |
| `heard_lines`, `heard_minutes` | 6, 60 | lines said to the seat that stay on its wakes, and for how long |
| `chat_turns` | 4 | turns of only talk with the same person before the wake asks the seat to let it rest |
| `chat_hard` | 8 | turns of only talk with the same person after which a line to them is held once, until something is done |
| `recent_notes` | 5 | the seat's own last turn notes on each wake; its plan (from `sleep(plan=...)`) is shown apart and replaced, never added to |
| `unanswered` | 3 | lines to one person with no word back from them before the say result says they may never answer |
| `repeat_window` | 900 | seconds in which a line that mostly repeats one said to the same listener is refused once |
| `default_until` | `to_me()` | the watch when the model gives none |
| `default_minutes`, `max_minutes` | 60, 480 | how long a sleep may run |
| `min_minutes` | 1 | the shortest sleep; 0 for a test world where nothing moves unless the seat does |
| `look_every` | 600 | seconds between quiet looks (no model) for owed lines, rule changes and owner answers |
| `wait_piece` | 30 | seconds per wait call while sleeping: how soon your messages and schedules land |
| `wait_cap` | 90 | seconds one wait call may hold (servers cap at 120; proxies often less) |
| `day_seconds` | 86400 | the world's day, for "day N" on the card |
| `entity` | | which body, when a grant names several |
| `think_tool` | false | offer a `think(thought)` tool that reaches nobody (Anthropic's think tool). On deepseek-v4-flash it was used in 2 of 9 calibration runs and changed nothing measurable |
| `imagine` | false | offer `imagine` and `reflect` ([imagination.md](imagination.md)); needs the `imagine` extra |
| `past` | false | offer `past`, a search over the seat's own deeds and what came of each, with the body's deeds from before the seat read from the world's history at first join ([how-it-works.md](how-it-works.md#two-memories)) |
| `crew` | | the other bodies a crew grant names, by name: deeds take `as=` one of them ([how-it-works.md](how-it-works.md#a-crew)) |
| `reflexes` | false | `sleep` takes rules (`reflexes=`) and `waiting_for=`, run by the judge while the seat sleeps ([how-it-works.md](how-it-works.md#reflexes)); needs `judge` |
| `judge` | | `provider:model` of a decision model behind an OpenRouter-style decisions endpoint, for example `openrouter:typesafe/jev-1.14`; the key is read as for `model` |
| `judge_url` | | the decisions endpoint, when it is not the provider's base with `/alpha/decisions` |
| `reflex_fire`, `reflex_near` | 0.8, 0.45 | p at or over which a rule runs; p at or over which the mind wakes with a note that a rule nearly matched |
| `reflex_verbs` | none | the verbs a rule may act with without the mind; empty means rules only say, sleep or wake |
| `imagine_trials`, `surprise_bits` | 40, 4 | trials per imagining (at least 20); surprise at or over which the wake asks the seat to reflect |
| `menu` | false | the wake as numbered lists in place of the scene card, and a `menu` tool that opens an item (`open="3.2"`) or shows more of a list (`more="4"`) ([how-it-works.md](how-it-works.md#menus)) |
| `menu_rank` | false | with `menu`: each list puts first what the seat's goals and notes name and what was just said to it, then the newest |
| `menu_preopen` | false | with `menu`: what a line said to the seat names (people, things, its notes, the older talk with the speaker, its own deeds) is opened on the wake, before the model asks |
| `menu_lists` | false | with `menu`: the talk with whoever spoke and the body's own deeds come as numbered lists from the world's log, one short line an event, each opened in full by its number |
| `strict_params` | false | an act with a param its verb does not declare (the world's affordances say what each verb takes) comes back unsent, with what the verb takes. For models that misname params: Corra sent `n` for drop's `count` and dropped a whole purse |
| `always_to_me` | true | every watch also wakes on words aimed at the character, whatever else it waits for |
| `away_cap` | 300 | the longest pause, in seconds, between tries while the server is away |
| `telemetry` | off | `{url, key_env, detail, machine, run}`: counts of turns, calls and spend posted to a telemetry server you run; `detail` counts (default), lines or full ([telemetry.md](telemetry.md)) |
| `keep_wakes` | 5 | wakes kept in `cache/wakes.json` for `bh calibrate` (0 keeps none) |
| `extra_prompt` | | your own words to this seat, appended to its instructions |
| `stuck_notes` | 0 | a clause that means the same as one in this many of the last 6 notes (names the seat knows taken out) gets one wake note: a lead that keeps going nowhere may not exist, so ask the owner (`tell(to='owner', kind='quest')`). Needs `embed`. 0 is off |
| `embed` | | the embedding model, for what is compared by meaning (now `stuck_notes`): `provider:model` over its `/embeddings` endpoint (for example `ollama:paraphrase-multilingual`, `openai:text-embedding-3-small`, or your own under `providers`), or `local:<model>` run in the harness by fastembed (the `embed` extra; for example `local:sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`). Pick a multilingual model when a world's notes may not be English. Its spend goes in the seat's ledger as `embed` |
| `embed_near` | 0.65 | the nearness at which two texts mean the same, on your model's scale. Measured on the two models above: one lead 0.72 to 0.75 (English or Chinese), 0.60 across mixed languages; other talk with the same friend 0.40 to 0.62. Other models want their own: `bh near "a" "b" "c" --model ...` |
| `cap`, `cap_hours` | none, 24 | dollars this seat may spend per window, or `"always"` (never paused, not even by the pool). Set in the home's config it is the default for every seat; `null` in a seat's own config means no cap of its own. A capped seat calls no model (turns and judge alike) and its body sleeps on its watch until the window has room. `bh cap` sets it |
| `cap_home`, `cap_home_hours` | none, 24 | home config only: one pool for every seat in the home together |

## Spend caps

The harness never decides which seats matter; whoever runs the home does. Nothing is capped until you set it.

```
bh cap                          every seat: spent, cap, left, paused or not; then the home pool
bh cap crier guard 0.50         these seats: $0.50 per 24 h each
bh cap keeper always            never paused, not even by the pool
bh cap crier off                no cap of its own (the pool still counts)
bh cap crier 2 --hours 168      $2 per week
bh cap --home-pool 3.00         one pool for the whole home; --home-pool off removes it
```

A change counts at once, without a restart. A paused seat says so once in its outbox, and when it plays again its wake tells it how many wakes it missed.

## Provider keys

A provider's key is read from its environment variable (`OPENROUTER_API_KEY`, `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`,
`GROQ_API_KEY`, `VLLM_API_KEY`), or from a `.env` file listed in `env_files`. Local providers (ollama, lmstudio,
llamacpp) need none. Keys are never printed, logged or shown to the model.

## An example

```json
{
  "model": "openrouter:deepseek/deepseek-v4-flash",
  "extra": {"provider": {"order": ["baidu/fp8", "deepinfra/fp8/flex"], "require_parameters": true}},
  "escalate": ["openrouter:openai/gpt-6-luna"],
  "default_minutes": 30,
  "max_minutes": 90
}
```

A cheap model for every turn, pinned to the endpoints whose price and cache you measured, a second model for the
turns where the first one keeps getting calls wrong, and sleeps no longer than an hour and a half.
