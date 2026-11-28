"""Meaning without a language: an embedding model turns a text into a vector, and texts that mean the same thing land
near each other in any script the model was trained on. The harness never reads the words; it asks how near two texts
are.

`embed` names the model the way `model` does: `<provider>:<model>` over any OpenAI-compatible `/embeddings` endpoint
(openrouter, openai, ollama, vllm, lmstudio, or your own under `providers`), or `local:<model>`, run in this process
by fastembed (the `embed` extra). Every model has its own scale of nearness, so the threshold that means "the same
thing" is set per model (`embed_near`) and checked with `bh near`.

Vectors live only in memory, keyed by their text; what a seat keeps on disk is text. A changed model re-embeds the
texts, so two models' vectors never meet."""

from __future__ import annotations

import asyncio
import math
import re
import time
from collections.abc import Iterable
from typing import Any

import httpx

from .providers import Model, ModelError
from .usage import Usage

KEEP = 4096  # vectors held per embedder; the oldest half goes when it fills
# Where "the same thing" starts. On paraphrase-multilingual (Ollama) and paraphrase-multilingual-MiniLM-L12-v2,
# a lead's clauses met 0.72 to 0.75 in English or Chinese and 0.60 in mixed languages; talk about other things
# with the same friend 0.40 to 0.62; unrelated days about 0.2. Other models want their own: `bh near`.
NEAR = 0.65


class Embedder:
    def __init__(self, spec: str, cfg: dict[str, Any] | None = None):
        provider, _, name = spec.partition(":")
        if not name:
            raise ModelError(f"name the embedding model: '<provider>:<model>' or 'local:<model>' (got {spec!r})")
        self.spec, self.name = spec, name
        self.model = None if provider == "local" else Model.from_config(spec, cfg)
        self._local: Any = None
        self.cache: dict[str, list[float]] = {}
        self.spent = Usage()  # since the last take(): the seat's ledger records it

    def take(self) -> Usage:
        u, self.spent = self.spent, Usage()
        return u

    async def vectors(self, client: httpx.AsyncClient, texts: list[str]) -> list[list[float]]:
        new = [t for t in dict.fromkeys(texts) if t not in self.cache]
        if new:
            for t, v in zip(new, await self._embed(client, new)):
                self.cache[t] = _unit(v)
            if len(self.cache) > KEEP:
                for k in list(self.cache)[:KEEP // 2]:
                    del self.cache[k]
        return [self.cache[t] for t in texts]

    async def _embed(self, client: httpx.AsyncClient, texts: list[str]) -> list[list[float]]:
        t0 = time.monotonic()
        if self.model is None:
            if self._local is None:
                try:
                    from fastembed import TextEmbedding
                except ImportError as e:
                    raise ModelError("local embeddings need the embed extra (fastembed): uv sync --extra embed") from e
                self._local = await asyncio.to_thread(TextEmbedding, self.name)
            out = await asyncio.to_thread(lambda: [[float(x) for x in v] for v in self._local.embed(texts)])
            self.spent.add(Usage.from_response({}, self.spec, time.monotonic() - t0, local=True))
            return out
        m = self.model
        headers = {"content-type": "application/json", **({"Authorization": f"Bearer {m.key}"} if m.key else {})}
        last = ""
        for attempt in range(3):
            try:
                r = await client.post(f"{m.base}/embeddings", json={"model": m.model, "input": texts},
                                      headers=headers, timeout=60)
            except httpx.HTTPError as e:
                last = f"{type(e).__name__}: {e}"
                await asyncio.sleep(2 ** attempt)
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                last = f"{r.status_code} {r.text[:300]}"
                await asyncio.sleep(2 ** attempt * 2)
                continue
            if r.status_code != 200:
                raise ModelError(f"{self.spec}: {r.status_code} {r.text[:500]}")
            data = r.json()
            rows = sorted(data.get("data") or [], key=lambda d: d.get("index", 0))
            if len(rows) != len(texts):
                raise ModelError(f"{self.spec}: {len(rows)} vectors for {len(texts)} texts: {str(data)[:300]}")
            self.spent.add(Usage.from_response(data.get("usage"), self.spec, time.monotonic() - t0, m.prices,
                                               m.local))
            return [d["embedding"] for d in rows]
        raise ModelError(f"{self.spec}: gave up after retries: {last}")


def _unit(v: Iterable[float]) -> list[float]:
    v = list(v)
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine of two unit vectors: 1 is the same meaning, and what counts as near depends on the model."""
    return sum(x * y for x, y in zip(a, b))


class SoftDict:
    """A dict whose keys match by meaning. `get(text)` gives the value of the nearest key at or over `near`, so a lead
    filed as "the archive runner" is found again as "a runner from the archive" or "Archivläufer". Keys stay text,
    so the dict saves as plain JSON and survives a change of model."""

    def __init__(self, embedder: Embedder, near: float, items: dict[str, Any] | None = None):
        self.e, self.near = embedder, near
        self.items: dict[str, Any] = dict(items or {})

    async def nearest(self, client: httpx.AsyncClient, text: str) -> tuple[str, float]:
        if not self.items:
            return "", 0.0
        keys = list(self.items)
        q, *vs = await self.e.vectors(client, [text, *keys])
        return max(((k, cosine(q, v)) for k, v in zip(keys, vs)), key=lambda kv: kv[1])

    async def get(self, client: httpx.AsyncClient, text: str, default: Any = None) -> Any:
        k, s = await self.nearest(client, text)
        return self.items[k] if k and s >= self.near else default

    async def has(self, client: httpx.AsyncClient, text: str) -> bool:
        k, s = await self.nearest(client, text)
        return bool(k) and s >= self.near

    def __setitem__(self, text: str, value: Any) -> None:
        self.items[text] = value

    def __len__(self) -> int:
        return len(self.items)


def clauses(text: str, names: Iterable[str] = ()) -> list[str]:
    """A text cut at its punctuation, in any script (。 and ， are punctuation as much as . and ,), with the names
    given taken out whole: a friend named in every note is not a lead."""
    t = text.casefold()
    for n in sorted({str(n).casefold() for n in names if n}, key=len, reverse=True):
        t = t.replace(n, " ")
    return [c for c in (" ".join(p.split()) for p in re.split(r"[^\w\s]+", t)) if len(c) >= 4]
