"""Spend caps: how much each seat may spend, chosen by whoever runs the home.

The harness never decides which seats matter. The operator does, with `bh cap`: a cap on a seat that matters little,
none on one that must keep going, and a pool for the whole home if they want one. A capped seat makes no model calls
(its turns and its reflex judge alike); its body stays in the world, asleep on its watch, and it plays again when
the window has room or the cap is raised. Nothing is capped unless the operator sets it.

In a seat's config.json: cap (dollars per window), cap_hours (the window, 24). The home's config.json may hold cap
and cap_hours too: a default for every seat that sets none of its own. cap_home and cap_home_hours there are one pool
for every seat in the home together. A seat whose cap is "always" is never paused, not even by the pool."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .seat import Seat, read_json, write_json
from .usage import fold

_SPENT: dict[tuple[str, float], tuple[float, float]] = {}  # (usage file, window) -> (when read, dollars)


def spent(p: Path, hours: float, now: float | None = None, fresh: bool = False) -> float:
    """Dollars a usage log shows for the last `hours`, read at most once a minute per file."""
    now = now or time.time()
    k = (str(p), hours)
    if not fresh and k in _SPENT and now - _SPENT[k][0] < 60:
        return _SPENT[k][1]
    v = fold([p], now - hours * 3600)["all"].cost
    _SPENT[k] = (now, v)
    return v


def _hours(v: Any) -> float:
    try:
        return max(0.1, float(v))
    except (TypeError, ValueError):
        return 24.0


def over(seat: Seat, now: float | None = None) -> str:
    """Why this seat may not call a model now, or "". Read fresh: a changed cap counts without a restart."""
    cfg = seat.config()
    cap = cfg.get("cap")
    if cap == "always":
        return ""
    if isinstance(cap, (int, float)):
        h = _hours(cfg.get("cap_hours", 24))
        s = spent(seat.p("usage.jsonl"), h, now)
        if s >= float(cap):
            return f"this seat spent ${s:.2f} in the last {h:g} h; its cap is ${float(cap):.2f}"
    home = read_json(seat.home / "config.json", {})
    pool = home.get("cap_home")
    if isinstance(pool, (int, float)):
        h = _hours(home.get("cap_home_hours", 24))
        s = sum(spent(p, h, now) for p in sorted((seat.home / "seats").glob("*/usage.jsonl")))
        if s >= float(pool):
            return f"the seats in this home spent ${s:.2f} in the last {h:g} h; the home's pool is ${float(pool):.2f}"
    return ""


def parse(v: str) -> float | str | None:
    """A cap as the operator types it: dollars ('0.50', '$2'), 'off' (none of its own) or 'always' (never paused)."""
    t = v.strip().lower().lstrip("$")
    if t in ("off", "none", "no"):
        return None
    if t in ("always", "never-pause", "keep-going"):
        return "always"
    x = float(t)
    if x < 0:
        raise ValueError("a cap is zero or more dollars")
    return x


def set_seat(seat: Seat, cap: float | str | None, hours: float | None = None) -> None:
    cfg = read_json(seat.p("config.json"), {})
    cfg["cap"] = cap  # null, not removed: a seat set to off is not capped by a home default either
    if hours is not None:
        cfg["cap_hours"] = hours
    write_json(seat.p("config.json"), cfg)


def set_home(home: Path, pool: float | None, hours: float | None = None) -> None:
    cfg = read_json(home / "config.json", {})
    if pool is None:
        cfg.pop("cap_home", None)
    else:
        cfg["cap_home"] = pool
    if hours is not None:
        cfg["cap_home_hours"] = hours
    write_json(home / "config.json", cfg)


def table(home: Path, seats: list[Seat], now: float | None = None) -> list[str]:
    """One line per seat: its cap, what it spent in its window, what is left, and whether it is paused now."""
    now = now or time.time()
    out = []
    for s in seats:
        cfg = s.config()
        cap, h = cfg.get("cap"), _hours(cfg.get("cap_hours", 24))
        used = spent(s.p("usage.jsonl"), h, now, fresh=True)
        why = over(s, now)
        if cap == "always":
            what = "always (never paused)"
        elif isinstance(cap, (int, float)):
            what = f"${float(cap):.2f}/{h:g}h, ${max(0.0, float(cap) - used):.2f} left"
        else:
            what = "none"
        out.append(f"{s.name:14} spent ${used:.4f} in {h:g}h   cap {what}" + ("   PAUSED" if why else ""))
    hc = read_json(home / "config.json", {})
    if isinstance(hc.get("cap_home"), (int, float)):
        h = _hours(hc.get("cap_home_hours", 24))
        used = sum(spent(s.p("usage.jsonl"), h, now, fresh=True) for s in seats)
        out.append(f"{'home pool':14} spent ${used:.4f} in {h:g}h   pool ${float(hc['cap_home']):.2f}, "
                   f"${max(0.0, float(hc['cap_home']) - used):.2f} left (seats set to 'always' are not paused by it)")
    else:
        out.append(f"{'home pool':14} none")
    return out
