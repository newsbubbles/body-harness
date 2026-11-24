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
- Every lesson from a failure in play becomes either a guard in code (a lint, a wrapper, a default) or a line in a
  skill, with the case that taught it in the commit message.
