---
name: waiting
description: The sleep condition grammar - what until= can read, the ladder from narrow to wide, and the traps that leave a seat blind or awake all night.
---

# Waiting well

`sleep(until="<condition>", minutes=N, note=...)` ends your turn. The world checks the condition every half second
against what you may know; the harness wakes you when it is true, when N minutes pass, when your operator writes,
when something you scheduled is due, or when a line is owed to you. The world's own page is `guide('watch')`.

## What a condition can read

| name | what it is |
|---|---|
| `to_me()` | someone spoke to you or called you by name as an address |
| `named()` | your name in anyone's line (busy places: fires a lot) |
| `heard(word, by='')` | a line with that word, optionally by someone whose name contains `by`; `heard('', by='Brakka')` is anything Brakka says, to anyone |
| `said_here()` | the harness's shorthand: anyone but you said something here |
| `arrived(name)` | someone of that name walked in since you slept (`''` for anyone) |
| `came(name)` | someone of that name is here now (true already? it fires at once) |
| `did(kind, by='')` | an event of that kind (`did('attack')`, `did('give', by='Corin')`) |
| `state(name)` | public state of someone here or a thing you hold |
| `t`, `ambient`, `here`, `present`, `me`, `events` | the hour in seconds, sky, your place, names here, your own state, what touched you |

Read your own state with a default: `me.get('hp', 9) < 3`. Names match by part, any case.

## The ladder: watch at the level you will answer at

1. `to_me()`: words aimed at you. Most characters need little more.
2. A named person or verb: `arrived('Reeve')`, `heard('water')`, `did('give', by='Corin')`.
3. A state crossing a line: `me.get('hp', 9) < 3`, `t >= 43200`.
4. `did(by='Corin')`: anything one person does.
5. `did()`: any deed by anyone. Only for a short busy scene (a fight, a haggle), never for a long wait.

Make it wide enough: `to_me()` or-ed with what your goal cares about in your place. A mind waiting on `to_me()`
alone slept through a player naming its goal aloud beside it.

## Traps

- A condition already true when you arm it fires at once. Wait for a change: write the hour you see now into it
  (`ambient.get('time_of_day') != 'dusk'`), or use `t >= <a number>`.
- Some hours never appear as a value (a world may skip `dawn`): wake on a `t` number instead.
- `heard('<your own name>')` wakes on your own lines.
- Events have `seq, t, kind, actor, target, text, player`; there is no `payload` in a condition.
- If the world refuses the condition, the refusal names what it can read: fix it and sleep again.

## Reflexes (when your sleep takes reflexes=)

Some seats can leave their body up to 5 rules for the sleep, so small things need no turn of yours. Each rule is
`if=` what happens, in words ("Brakka greets me"), an optional `unless=`, and `do=`: say (with `text=` written now
and `to=`), act (go, follow, rest and the like), sleep (let it pass) or wake. Say in `waiting_for=` what you wait
for and why: a small judge reads every rule against it, runs a rule only when it is clearly true, and wakes you
when it is unsure. Anything no rule takes wakes you as usual; coin, giving, fighting and promises always do.

- Write rules for the small things you can answer now: a greeting, a regular passing through, the same news twice.
- Keep `if=` narrow: "Brakka greets me" fires far better than "someone wants something".
- Your next wake says what your rules did while you slept. Read it before you answer anyone.
