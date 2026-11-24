---
name: apps
description: Building your own tools for playing this character - goal trackers, ledgers, mood, maps - as small Python apps in your seat that become callable tools.
---

# Your apps

An app is a small Python file in your seat's `apps/` folder that becomes a tool you can find and call. Build one
when you keep doing the same bookkeeping by hand: a tally of debts, a mood that drifts with what happens to you, a
map of the places you have walked, a ledger of promises.

## The shape

```python
# apps/debts.py
TOOL = {
    "description": "My ledger of who owes me what. op: add (who, what), pay (who), list.",
    "parameters": {"type": "object", "properties": {
        "op": {"type": "string", "enum": ["add", "pay", "list"]},
        "who": {"type": "string"}, "what": {"type": "string"}}, "required": ["op"]},
}

import json, os
DATA = os.path.join(os.environ["BH_DATA"], "debts.json")

def run(args):
    book = json.load(open(DATA)) if os.path.exists(DATA) else {}
    if args["op"] == "add":
        book.setdefault(args["who"], []).append(args["what"])
    elif args["op"] == "pay":
        book.pop(args["who"], None)
    json.dump(book, open(DATA, "w"))
    return book
```

- Write it with `write_file(path='apps/debts.py', text=...)`, then `find_tools('debts')` loads it as `app_debts`.
- `TOOL` must be a plain literal (it is read without running the file). `run(args)` returns text or JSON.
- `BH_DATA` is the app's own folder for its files. `BH_SCENE` is the last full view of the world (JSON): read it
  for where you are, who is here, your card. An app never talks to the world itself: you do that.
- Apps and `run_python` run in your sandbox: your workspace and apps folders, no network, no other processes.

## Good apps

- Keep the world's numbers in the world: an app may cache them, but look() decides.
- Append-only logs beat rewritten files: you can always see how a number came to be.
- One app, one job. A short description that says the ops, so find_tools finds it.
- Test it once with run_python or by calling it, then memo what it is for.
