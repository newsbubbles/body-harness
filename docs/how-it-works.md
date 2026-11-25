# How body-harness works

What happens inside a seat, for people who run seats and agents who set them up. The commands are in the
[README](../README.md); every setting is in [configuration.md](configuration.md).

## Why a harness of its own

Minds in embody worlds have run as general coding-agent sessions. That works, and it costs: a full `perceive` in a
busy world is 50K to 250K characters, a background watcher started the wrong way dies silently, a short name
(`say(to="Taan")`) or a wrong parameter (`inspect(target=)`) is refused, and every footstep in a long wait can cost a
model turn. body-harness is built around those lessons:

- **A scene card, not the whole world.** Each turn the model reads a few hundred tokens: the hour, the place, who is
  here, what it holds, the exits, its goals, any line it owes, the verbs it can use, and what happened (a routine's
  bark said twelve times is one line, x12). The full view stays on disk; `look(part=...)` reads any piece of it.
- **The harness owns the wait.** The model ends each turn with `sleep(until=..., note=...)`. The harness then waits
  on the world itself, with no model calls, in pieces under the server's cap, and wakes the model only when the
  condition fires, the time is up, the operator writes, something scheduled comes due, a player has waited on an
  answer, the owner answered, or the rules changed. It rejoins a lapsed seat, resets standing orders after deeds, and
  backs off with jitter when the server is away. No shell watchers, no `&`.
- **Fresh short turns.** A turn is the stable system prompt (brief, rules, skill and handbook indexes, which the
  provider can cache) plus one wake message. Nothing grows across days; continuity lives in the world's memory, the
  seat's notebook, its plan and the last few turns' notes.
- **Ten tools loaded, the rest found.** `look, act, say, remember, intend, sleep, find_tools, tell, memo, skill`.
  `find_tools(query)` searches the world's other ops (from the server's own OpenAPI description), the harness's
  extras and the seat's apps, and loads what it finds for the next few turns. `think`, `imagine` and `reflect` are
  added when a seat's config turns them on.
- **Exact calls.** Short names resolve to the full name the engine wants, from who is present and everyone seen;
  wrong parameter names are mapped; a refusal comes back as reason, hint and what would work instead. A tool name the
  model made up is read as the tool it meant (`speak` as `say`, `walk` as `act go`) and logged.

## Guards learned in play

Each of these came from a seat doing the wrong thing in a real world, and is now code:

- **Words are not deeds.** A line that claims a deed no tool did ("I hand you the key") gets a note back: act first.
- **Plans are replaced, not piled up.** `sleep(plan=...)` replaces the seat's plan; the wake shows it apart from the
  last few notes, so an old plan does not outlive the correction that changed it.
- **Talk that goes nowhere rests.** Two seats that wake on each other's words get asked to let it rest after a few
  turns of only talk, and a line is held once after more.
- **Nobody home.** After a few lines to someone with no answer, the seat is told some bodies have no mind behind them.
- **Cut-off replies get room.** A thinking model cut off before it said anything gets more tokens, once.
- **Escalation.** After repeated wrong calls in one turn, or many reads with no deed, the rest of the turn goes to the
  next model on the seat's list; the next turn starts on its own model again.

## Two memories

- **The character's**, kept by the world: `remember` (secret notes), `intend` (goals with reasons), `recall` (what
  this body knows of someone). Whoever holds the body next reads them, on any machine.
- **The harness's**, kept by the seat: `memo`, markdown files with a `MEMORY.md` index. The `seat` shelf is this
  character's (how its people answer, what its operator wants); the `craft` shelf is shared by every seat you run
  (lessons about playing well). Numbers about the world always come from the world, never from notes.

Neither lines moments up. "Every time I struck twice running, what did Oren do next?" is a question about many
moments at once. With `past=true` a seat gets `past`, a search over every deed it did (act, say) and what came of
each, read from its own `calls.jsonl`. It costs no model call. On a real Dojo seat's log, after its 50 turns:

```
past(seq=['strike', 'strike'], count='next.they')
3 of your 46 deeds (#1-#46) match run strike > strike.
count next.they: guard 2/3, special 1/3
#38 t40 strike Oren -> you=strike they=throw dealt=3 took=0
#42 t44 strike Oren -> you=strike they=guard dealt=0 took=3
#43 t46 strike Oren -> you=strike they=guard dealt=0 took=3
```

`verb=` picks deeds, `where=` filters fields of the result (`{'they': 'guard', 'took': '>0'}`), `match=` is a regex
over the whole moment, `seq=` a run of verbs in a row, `count=` a tally over every match, `before=`/`after=` context
around each match, `last=` only recent deeds. A field that does not exist answers with the fields that do.

A body can live in a world before it gets a seat. On its first join with `past=true`, the seat reads that body's
own deeds from the world's `history(touching=<body>)` into `past_world.json`, with what each caused, so `past`
searches them too (turn `d61` is world day 61). `bh past-import N` reads them again. Where the seat's own log
starts inside the world's copy, the harness finds its first line said, word for word, so no deed counts twice.

A body can also come with notes from before: a digest of its old sessions, its memory files, a start brief.
`bh import <seat> <path>` links a file or folder into the seat's `import/` folder, so nothing is duplicated and
nothing moves (a folder as a symlink, or a junction on Windows; a file that cannot be linked as a small pointer
file). `--copy` copies instead, `--unzip` unpacks a zip archive, `--list` and `--remove` manage them. Removing a
link never touches what it pointed at. With anything imported, the seat gets `records`: `match=` searches every
line of every text file the folder reaches, following links, and `file=` reads one. A token or `.env` file in an
imported folder is never read.

## Outside the fiction

`tell` is one tool for both lines out of the world:

- `tell(to='owner', kind=..., what=..., why=...)` reaches the world's owner: `flaw` (or `vulnerability`) for a hole
  in the rules, `capability` (or `feature`) for a missing verb, `fix`, `proposal`, `quest`, `help`, `seat`,
  `testing`. Answers come back in the scene.
- `tell(to='operator', ...)` reaches you, the human running the seat. Read it with `bh outbox <seat>`; write back
  with `bh tell <seat> "..."`, which wakes the seat.

## A mind with a builder's hand

A world may give one character a mind grant on its body and a builder grant on one part of the world (its plot: a
place and all below it, the types it may make, a prefix for its scripts). The engine holds a mind grant to its seat
and takes no limits on it, so the hand is a second grant. `bh hand <seat> add <name> <invite>` keeps it in
`hands/<name>.json`, beside `door.json`. From the seat's next start, an op the mind's grant lacks (spawn, patch,
move, layout, program...) goes through a hand that has it, and everything else through the mind's door. A hand that
refuses for reach ("your grant reaches only under ...") passes the op to the next hand. The one that did the deed is
tried first next time on the same target. A refusal names every hand tried. `bh hand <seat> list` shows each hand's
role and op count, and `bh hand <seat> remove <name>` puts the file aside (the grant itself lives until the world's
owner revokes it).

## Tasks, schedule, accounting

The wake message carries an agenda: lines owed to players first, then your messages, the owner's answers, scheduled
items and the character's goals. `schedule` (found with find_tools) wakes the seat at a world time or after real
minutes. Every turn is one line in `ledger.jsonl`: why it woke, each call and what the world said, the note, the
watch it set, and what it cost. A tool name the model made up is logged to `invented.jsonl` with how the harness read
it, so you can see which models guess names.

## Usage and caching

One `Usage` record for every provider: input tokens, cached tokens, cache writes, output and reasoning tokens, cost,
and wall time. OpenRouter reports cost itself; local providers cost nothing; anything else is priced from `prices`
in the config or flagged as unpriced. `bh usage --since 24h` sums it by seat and model.

For caching on OpenRouter, each seat sends a stable `session_id` (so its calls stay on one provider and the cache
stays warm), the system prompt never changes between wakes, and models that need a breakpoint (Anthropic, Gemini,
Qwen) get one at the end of the system prompt. `cache=off` or `cache=on` in a seat's config overrides that. To keep a
seat on endpoints you chose, set `extra` (see [configuration.md](configuration.md)).

## Apps and the sandbox

A character can build its own tools: a debt ledger, a mood that drifts with what happens to it, a map of where it
has walked. An app is `apps/<name>.py` with a `TOOL` literal (description, JSON schema) and `run(args)`; it becomes
`app_<name>`, findable like any tool. `run_python` and apps run in a child process with an audit hook that keeps them
to the seat's workspace and apps folders: no network, no other processes, no native calls, no reading the door token
or other seats. It is a guard rail, not a VM: if you run seats for people you do not trust, run the harness in a
container.

## Skills

Built in: `mind`, `player`, `waiting`, `owner`, `apps`. Add your own as `<name>/SKILL.md` (frontmatter with `name`
and `description`) under `$BH_HOME/skills/` for every seat or `seats/<name>/skills/` for one. Only the index rides
each turn. The world's own handbook pages for the seat are listed in the prompt and read on demand, so a big
handbook costs nothing until a page is needed.

## What a seat keeps

Everything is a file under `$BH_HOME/seats/<name>/`:

| file | what |
|---|---|
| `door.json` | the world's address and the seat's token (never shown to the model, logs or sandbox) |
| `import/` | files and folders its operator gave it, mostly links ([bh import](#two-memories)) |
| `hands/` | extra grants on the same world, one door file each ([a builder's hand](#a-mind-with-a-builders-hand)) |
| `config.json` | the seat's settings |
| `state.json` | who it is, its plan, last notes, what it heard, its watch |
| `ledger.jsonl` | one line per turn: reason, calls, note, watch, cost |
| `calls.jsonl` | every tool call and what came back |
| `usage.jsonl` | every model call's tokens and cost |
| `invented.jsonl` | tool names the model made up, and how they were read |
| `inbox.jsonl`, `outbox.jsonl` | your messages to the seat, and its reports to you |
| `memory/`, `workspace/`, `apps/`, `skills/` | its notes, files, tools and skills |
| `cache/wakes.json` | its last few wakes as they arrived, for `bh calibrate` |
| `imagination.json`, `predictions.jsonl` | its imagination, when on ([imagination.md](imagination.md)) |
