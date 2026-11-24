"""One usage record for every provider: tokens in, tokens read from cache, tokens out, and money.

Providers name these differently (OpenAI's prompt_tokens_details.cached_tokens, OpenRouter's cost and
cache_write_tokens, Ollama's bare prompt_tokens, Anthropic's input/output_tokens). `Usage.from_response` reads any of
them into the same fields, so a seat's ledger, `bh usage` and a price check never care where a call went."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# Dollars per million tokens (input, cached input, output) for models whose provider does not report cost.
# A seat's config may add its own under "prices"; OpenRouter reports cost itself, and local models cost 0.
PRICES: dict[str, tuple[float, float, float]] = {}


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0  # every prompt token, cached or not
    cached_tokens: int = 0  # of input_tokens, read from the provider's cache
    cache_write_tokens: int = 0  # of input_tokens, written to the cache (priced higher by some providers)
    output_tokens: int = 0
    reasoning_tokens: int = 0  # of output_tokens, spent thinking
    cost: float = 0.0  # dollars, as reported or priced
    cost_known: bool = True  # false once any call's cost had to be guessed as 0
    seconds: float = 0.0  # wall time spent waiting on the model
    by_model: dict[str, dict[str, float]] = field(default_factory=dict)

    @classmethod
    def from_response(cls, u: dict[str, Any] | None, model: str = "", seconds: float = 0.0,
                      prices: dict[str, Any] | None = None, local: bool = False) -> "Usage":
        u = u or {}
        pin = u.get("prompt_tokens_details") or u.get("input_tokens_details") or {}
        pout = u.get("completion_tokens_details") or u.get("output_tokens_details") or {}
        inp = int(u.get("prompt_tokens") or u.get("input_tokens") or 0)
        # Anthropic counts cache reads and writes beside input_tokens, not inside it
        a_read, a_write = int(u.get("cache_read_input_tokens") or 0), int(u.get("cache_creation_input_tokens") or 0)
        if a_read or a_write:
            inp += a_read + a_write
        cached = int(pin.get("cached_tokens") or a_read or 0)
        written = int(pin.get("cache_write_tokens") or a_write or 0)
        out = int(u.get("completion_tokens") or u.get("output_tokens") or 0)
        reasoning = int(pout.get("reasoning_tokens") or 0)
        cost, known = u.get("cost"), True
        if cost is None:
            price = (prices or {}).get(model) or PRICES.get(model)
            if price:
                p_in, p_cached, p_out = price
                cost = ((inp - cached) * p_in + cached * p_cached + out * p_out) / 1e6
            elif local:
                cost = 0.0
            else:
                cost, known = 0.0, False
        r = cls(1, inp, cached, written, out, reasoning, float(cost), known, seconds)
        r.by_model = {model or "?": r._row()}
        return r

    def _row(self) -> dict[str, float]:
        return {"calls": self.calls, "input_tokens": self.input_tokens, "cached_tokens": self.cached_tokens,
                "output_tokens": self.output_tokens, "cost": self.cost}

    def add(self, other: "Usage") -> "Usage":
        for k in ("calls", "input_tokens", "cached_tokens", "cache_write_tokens", "output_tokens", "reasoning_tokens",
                  "cost", "seconds"):
            setattr(self, k, getattr(self, k) + getattr(other, k))
        self.cost_known = self.cost_known and other.cost_known
        for m, row in other.by_model.items():
            mine = self.by_model.setdefault(m, {k: 0 for k in row})
            for k, v in row.items():
                mine[k] = mine.get(k, 0) + v
        return self

    @property
    def cache_rate(self) -> float:
        return self.cached_tokens / self.input_tokens if self.input_tokens else 0.0

    def line(self) -> str:
        cost = f"${self.cost:.4f}" + ("" if self.cost_known else " (some calls unpriced)")
        return (f"{self.calls} model calls, {self.input_tokens:,} in ({self.cache_rate:.0%} cached), "
                f"{self.output_tokens:,} out, {cost}, {self.seconds:.0f}s")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Usage":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def append(path: Path, seat: str, model: str, usage: Usage, reason: str = "") -> None:
    """One line per model call into a seat's usage log, for `bh usage` to fold by seat, model and day."""
    row = {"ts": time.time(), "seat": seat, "model": model, "reason": reason, **usage.to_dict()}
    row.pop("by_model", None)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def fold(paths: list[Path], since: float = 0.0) -> dict[str, Usage]:
    """Sum usage logs into one Usage per seat, plus "all"."""
    out: dict[str, Usage] = {"all": Usage()}
    for p in paths:
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("ts", 0) < since:
                continue
            u = Usage.from_dict(row)
            u.by_model = {row.get("model", "?"): u._row()}
            out.setdefault(row.get("seat", "?"), Usage()).add(u)
            out["all"].add(u)
    return out
