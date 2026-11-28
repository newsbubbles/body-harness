# Working on body-harness

For any agent or person changing this code.

- Python 3.11+, one dependency (httpx). The `imagine` extra adds the embody engine (pure Python, no dependencies
  of its own); without it the imagine tools are simply not offered. `uv sync`, then `uv run --group dev pytest -q`.
- The harness talks to a world only through `POST <base>/op/<name>` (door.py). It never reads a world's files, and a
  seat's token never reaches the model, the logs or the sandbox.
- Small on purpose. Before adding a module or a dependency, check whether the world already answers it (an op, a
  handbook page) or a skill can teach it. What belongs in the engine (a smaller perceive, a has-mind flag) is asked
  of the engine, not faked here for long.
- The system prompt must stay byte-stable between wakes (prompt caching): anything that changes per wake goes in
  the wake message (prompts.wake).
- Seats live in `$BH_HOME`, never in the repository. No tokens, personal paths, addresses or world secrets in
  tracked files, tests or skills.
- Built-in skills are harness-neutral and world-neutral: a lesson every embody world shares, never one world's
  house rules or story. A world's own rules come from its handbook.
- **No word lists.** The harness never branches on words or names a world may or may not use: no lists of verb
  names, state keys, titles or item words, no English phrase patterns over what a model wrote, no fuzzy renames of
  what it sent. What a world has comes from what it declares (affordances and their params, schemas, event kinds,
  refusals). When that is missing, the ask goes to the engine, not into a list here. `tests/test_no_word_lists.py`
  fails on any new word list or word pattern in `src/`, and on a word added to an old one; `tests/word_lists.json`
  holds the reviewed ones, each marked contract (the engine's or the harness's own names) or debt (guesses still to
  remove, from docs/audit-string-conditionals.md). Never add debt to it. What stays variable, always read from the
  world: names and any text of a world; entity types (a world has as many as its schemas say); limits the world or
  the seat's owner marks. A harness default is not a fix for a world's or a model's behaviour.
- Fix at the source. When the cause is in the engine or a world, the fix goes there, proposed with restraint and
  approved by the owner or the agent the owner delegates that authority to; a harness patch around it is not the fix.
- Every lesson from a failure in play becomes either a guard in code (a lint, a wrapper, a default) or a line in a
  skill, with the case that taught it in the commit message.
