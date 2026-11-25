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
| `budget_day` | none | dollars this seat may spend on its model over any 24 hours; past it the seat pauses (no model calls), says so once in its outbox and plays again when the window has room. Read fresh, so `bh config` takes effect without a restart |
| `budget_home_day` | none | the same cap for every seat in the home together; set it in the home's `config.json` |
| `reflexes` | false | `sleep` takes rules (`reflexes=`) and `waiting_for=`, run by the judge while the seat sleeps ([how-it-works.md](how-it-works.md#reflexes)); needs `judge` |
| `judge` | | `provider:model` of a decision model behind an OpenRouter-style decisions endpoint, for example `openrouter:typesafe/jev-1.14`; the key is read as for `model` |
| `judge_url` | | the decisions endpoint, when it is not the provider's base with `/alpha/decisions` |
| `reflex_fire`, `reflex_near` | 0.8, 0.45 | p at or over which a rule runs; p at or over which the mind wakes with a note that a rule nearly matched |
| `reflex_verbs` | go, follow, rest, sit, stand, look, wait | the deeds a rule may do without the mind |
| `imagine_trials`, `surprise_bits` | 40, 4 | trials per imagining (at least 20); surprise at or over which the wake asks the seat to reflect |
| `keep_wakes` | 5 | wakes kept in `cache/wakes.json` for `bh calibrate` (0 keeps none) |
| `extra_prompt` | | your own words to this seat, appended to its instructions |

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
