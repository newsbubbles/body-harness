# Audit: conditionals keyed on words and strings

28 Nov 2026. For Nate's review.

**The rule:** the harness must not branch on words or names that a world may or may not use. That covers guessed
verb names, guessed state keys, title words, English phrase patterns, and fuzzy renames. What the harness does should
come from what the world declares (affordances and their params, schemas, refusals) or from structure (counts, ids,
order). If the fix belongs to the engine, ask the engine for it instead.

I went through every regex, every literal word set and every string comparison in `src/body_harness` (7,900 lines).
Below are the cases, worst first, each with where it is, what it does, where it came from and what I recommend.

## Status, 28 Nov (after Nate's go-ahead)

Fixed:

- 1.1 reverted (4e98594). The declared-params check is now the `strict_params` setting, off by default.
- 1.2 deed claims, 2.1 `finish_first`, 2.2 hearsay: removed, with their tests.
- 1.3 reflexes: no default verbs. A rule acts only with verbs the seat's `reflex_verbs` lists; otherwise it says,
  sleeps or wakes.
- 1.4 imagine: the starter rule is named `YOUR_VERB`, and its hint lists the seat's own verbs to rename it to.
- 1.5 vitals: the engine's card fields (`condition`, `fading`, `decay`, `asleep`), then every state number the world
  gives a `max_` for.
- 1.6 titles: a name matches whole, casefolded, in any script (`scene.mentioned`). Whom a line is to is its `to`
  only, as in the engine (words-out). "Tam" no longer finds "Old Tam".
- 1.7 calibrate: a say, or an act that carries text.
- 2.3 `stuck_on`: by meaning, on an embedding model the operator picks (`embed`), so notes in any language work.
  Notes are cut at punctuation and lose known names whole; a clause near one in `stuck_notes` of the last 6 notes is
  the lead, and leads already told are matched by meaning too. Off unless `stuck_notes` and `embed` are set.
- 2.4 and 2.5: greeting words and phrase hints removed; being addressed is read from where the name stands in the line.
- 2.6 menu stop words removed; ranking matches the world's names whole.
- 3.2 world-verb routes removed (`walk`, `move`, `go_to`, `goto`, `travel`), and the "follow" hint with them.
- 4.1 `CALL_TROUBLE`: the harness's own call errors, plus the engine's `layer` (call or target). No engine wording.
- 4.2 a lost seat is the engine's `code` (`not_joined`, `seat_released`), never words in the reason. Worlds on an
  engine older than 26 Nov send no code, so their seats do not rejoin by themselves.
- The timer note keeps the world's own wording.

The engine debt is paid (engine fb29150): grant refusals carry `out_of_grant`, script errors `script_error`, a bad
`until` has `layer="call"`, `bad_args` names its `param`, and the briefing gives `pace_rate`. The harness reads those
codes and no longer any of the engine's wording.

A second pass took out English shapes as well as English words: ASCII letter classes (`[a-z]`), word splitting,
first-name keys and "word" lengths. Repeats compare runs of 5 characters, a person is a card with a `condition` or
`embodied_by`, and gauges are any `n/max`. The lint (`tests/test_no_word_lists.py`) now flags 2-item lists and ASCII
letter classes. Nothing is left on the debt list.

## 1. Guesses about a world's content

These guess what a world calls things. Each one fixes the case that taught it, misses the next world's wording, and
can quietly change what the model meant.

| # | where | what it keys on | came from | recommend |
|---|---|---|---|---|
| 1.1 | `tools.py` QUANTITY + `difflib` in `fix_args` | params named n, qty, amount, ... renamed to `count`, and any param within 0.8 similarity renamed to a declared one | 8b2c7ef, today: Corra's `drop(params={"n": 1})` dropped 57 coins | **Revert.** The engine accepted a param drop does not take, without a word. Asked Engine Dev to refuse it and name what the verb takes. The model then corrects itself from the refusal |
| 1.2 | `loop.py` `_CLAIMS`, `_deed_claim` | English regexes ("I take the job", "here's your coin", "I bought") mapped to guessed verbs `take_job`, `accept`, `hire`, `give`, `pay`, `buy` | 355cdab, 24 Nov: Ssareth said "I take it" and never ran take_job | **Remove.** World verbs and English phrasing hard-coded together |
| 1.3 | `reflexes.py` `SAFE_VERBS` | default reflex verbs `go, follow, rest, sit, stand, look, wait` | 51e1542, 25 Nov | **Remove the default.** Use only `reflex_verbs` from the seat's config, or ask the engine to mark verbs that are safe to automate |
| 1.4 | `imagine.py` `FIGHT` | `attack, spar, fight, strike, hit, duel, brawl` to pick which world verb names the starter rule, with "attack" as the fallback | 2213e26, 24 Nov: Tamsin's world said spar, not attack | **Remove.** Name the starter rule after a verb the seat actually has, or leave it unnamed |
| 1.5 | `scene.py` vitals | state keys `hp, water, food, wounded, asleep, bleeding, tired` shown as the body's vitals | scene card | **Replace** with what the world declares: ask the engine for a vitals flag on schema fields, or show every scalar field of the body's state |
| 1.6 | `scene.py` `_TITLES` | `Captain, Reeve, Elder, Sir, Lady, Master, Mistress, Brother, Sister, Old, Young, The` skipped when matching names in text | 16e36e0, 24 Nov | **Remove.** These are Glass Reach titles. Match the full name, or the name parts the engine gives |
| 1.7 | `calibrate.py` | `talk, ask, tell` counted as speaking deeds | calibration metric | **Fix:** count `say` ops and `talked` events only (both engine contract) |

## 2. English rules over the model's own words

These read the seat's notes or lines with English patterns. They are bound to one language and one way of phrasing,
and they grow by one case at a time.

| # | where | what it keys on | came from | recommend |
|---|---|---|---|---|
| 2.1 | `loop.py` `_INTENT`, `next_step`, `past_claim` (the `finish_first` setting) | "will", "next", "going to", "need to", ... and past forms of verbs ("fitted", "picked up") in the sleep note | 27 Nov, me, from the Shape Table runs | **Remove.** Off by default, and the study measured no clear effect (11/27 to 15/27, inside the noise; nothing on top of 32 steps) |
| 2.2 | `loop.py` `_hearsay`, `_TOLD`, `_PLAIN` | "said", "told me", "reckons", "he/she said", and an English stop-word list, to catch a seat quoting someone who never said it | defdceb, 25 Nov: Pip misquoted Captain Ruhl | **Rework or remove.** The fact it checks (did that person ever say this?) is real, but the detection is English phrasing. A structural form: show the seat the last lines it heard from anyone it names, and let the model judge |
| 2.3 | `loop.py` `stuck_on` | two-word phrases repeated in 5 of the last 6 notes, skipping `_PLAIN` stop words | Ongo's made-up archive runner, 25 Nov | **Keep the count, drop the stop-word list.** Repetition is structural, the English stop words are not |
| 2.4 | `behavior.py` `_HAIL` and the `named()` helper | "hey, oi, hi, hello, ho, listen, look, you" before a name counts as being spoken to | behaviour rules | **Rework** on the engine's `to` field (who a line was said to) instead of the words in it |
| 2.5 | `behavior.py` `_EXACT` | lint hints when a behaviour's text says "arrives", "leaves", "dusk", "says" | behaviour linter | **Low stakes** (a hint, never a change to a deed). Keep or drop, your call |
| 2.6 | `menu.py` `STOP`, `LOOSE_STOP` | English stop words when ranking list items by the seat's goals | the menu setting | **Low stakes**, off by default (menu hurt small models in the study). Remove if the menu goes |

## 3. Renames for tool and param names the model slipped on

These key on the harness's own tools, not on the world. A model sends `say(message=...)` and the harness reads it as
`say(text=...)`. That is the harness fixing calls to its own schema, which I think is in scope. One part crosses into
the world:

| # | where | what | recommend |
|---|---|---|---|
| 3.1 | `tools.py` `ALIASES`, `_TEXT_KEYS`, `_PLACE_KEYS`, stray args moved into params | wrong names for the harness's own tool params | **Keep** (harness schema), but the list should not keep growing: a wrong name should get a refusal that names the right one |
| 3.2 | `tools.py` `TOOL_ROUTES` | invented tools read as real ones: `emote` as a gesture, `whisper` as a quiet say, and **`walk`, `move`, `travel` as the world verb `go`** | **Drop the routes to world verbs** (walk to go). They are only used when the world has `go`, but they still guess its name. Keep the routes to the harness's own tools |

## 4. Matching the engine's error text

These read the wording of the engine's (or Python's) error messages. They are not world content, but they break as
soon as a message is reworded.

| # | where | what | recommend |
|---|---|---|---|
| 4.1 | `loop.py` `CALL_TROUBLE` | "bad arguments", "there is no tool", "unexpected keyword argument", ... to tell a bad call from a refusal | The engine already sends `layer: "call"`. **Use that only**, and ask the engine for error codes where it has none |
| 4.2 | `loop.py` seat-lapsed check | "join", "seat" or "embod" in a refusal's reason means the seat lapsed | **Ask the engine for a code** for "not seated" |
| 4.3 | `loop.py` `_OUT_OF_REACH`; `imagine.py` `crashed()` and the missing-key check | "your grant", "start with"; "failed while", "is not defined", "out of fuel", "key '...' not found" | **Same:** error codes from the engine |

## 5. Checked and fine

These name the engine's own contract or the harness's own internals, not a world:

- event kinds (`say`, `talked`, `moved`, ...) and op names (`act`, `say`, `look`, `perceive`, ...), and the bundle's
  fields (`present`, `holding`, `exits`, ...)
- the sandbox's deny lists, the secret-file and secret-key patterns, and telemetry's field checks
- JSON repair in `parse_args`, id and file name patterns
- `providers.py` `BREAKPOINT_MODELS` (which providers take cache breakpoints). This is a list of model families and
  could move to config, but it does not touch worlds
- `scene.py` `NOISE`, `menu.py` `BOOKKEEPING`: event kinds the engine defines

## What I propose

1. Revert 8b2c7ef (1.1) now. The engine ask for refusing undeclared params is already with Engine Dev.
2. Remove 1.2, 1.3, 1.4, 1.6, 2.1 and the world-verb routes in 3.2.
3. Ask the engine for vitals marked in the schema (1.5), a `to` on lines for 2.4, and error codes for section 4.
   Then switch the harness over as each lands.
4. Rework 2.2 and 2.3 into the structural forms described above.
5. From now on: any guard I add says what declared data or structure it reads. If the answer is "a word I picked", it
   does not go in.

On by default, so they change what live seats do today: 1.1, 1.2, 1.5, 1.6, 2.2, 2.3, and all of section 3. Behind
settings that are off by default: 1.3 (reflexes), 1.4 (imagine), 2.1 (finish_first), 2.6 (menu). The rest only give
hints or count things for calibration.
