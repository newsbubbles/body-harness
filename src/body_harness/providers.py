"""Models over the OpenAI-compatible chat API: OpenRouter, Ollama, vLLM, LM Studio, llama.cpp, or any base URL.

A model is named `<provider>:<model>`, for example `openrouter:deepseek/deepseek-chat-v3.1` or `ollama:qwen3:8b`.
A provider is a base URL and the name of the environment variable that holds its key. Every call returns the
assistant message and a `Usage`."""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .usage import Usage

PROVIDERS: dict[str, dict[str, Any]] = {
    "openrouter": {"base": "https://openrouter.ai/api/v1", "key": "OPENROUTER_API_KEY"},
    "ollama": {"base": "http://127.0.0.1:11434/v1", "key": "", "local": True},
    "vllm": {"base": "http://127.0.0.1:8000/v1", "key": "VLLM_API_KEY", "local": True},
    "lmstudio": {"base": "http://127.0.0.1:1234/v1", "key": "", "local": True},
    "llamacpp": {"base": "http://127.0.0.1:8080/v1", "key": "", "local": True},
    "openai": {"base": "https://api.openai.com/v1", "key": "OPENAI_API_KEY"},
    "deepseek": {"base": "https://api.deepseek.com/v1", "key": "DEEPSEEK_API_KEY"},
    "groq": {"base": "https://api.groq.com/openai/v1", "key": "GROQ_API_KEY"},
}

# OpenRouter models that cache only where a breakpoint marks the prefix; the rest cache on their own.
BREAKPOINT_MODELS = ("anthropic/", "google/gemini", "qwen/")


class ModelError(RuntimeError):
    pass


@dataclass
class Model:
    spec: str  # provider:model
    base: str
    key: str
    model: str
    provider: str
    local: bool = False
    temperature: float | None = None
    max_tokens: int = 1200
    reasoning: str = ""  # OpenRouter reasoning effort: low, medium, high ("" leaves the model's default)
    cache: str = "auto"  # auto: breakpoints where the model needs them; on: always; off: never
    session: str = ""  # OpenRouter session_id: keeps a seat's calls on one provider, so its cache stays warm
    prices: dict[str, Any] | None = None
    extra: dict[str, Any] | None = None  # passed through in the request body (for example Ollama's options)

    @classmethod
    def from_config(cls, spec: str, cfg: dict[str, Any] | None = None) -> "Model":
        cfg = cfg or {}
        provider, _, model = spec.partition(":")
        if not model:
            raise ModelError(f"name the provider: '<provider>:<model>', one of {', '.join(PROVIDERS)} or a base url "
                             f"in the seat's config (got {spec!r})")
        p = dict(PROVIDERS.get(provider, {}))
        p.update(cfg.get("providers", {}).get(provider, {}))
        if not p.get("base"):
            raise ModelError(f"unknown provider {provider!r}: add providers.{provider}.base to the config")
        key = os.environ.get(p["key"], "") if p.get("key") else ""
        if not key and p.get("key") and not p.get("local"):
            key = _key_from_env_files(p["key"], cfg.get("env_files", []))
        if not key and p.get("key") and not p.get("local"):
            raise ModelError(f"no key for {provider}: set {p['key']} in the environment or name a .env file in "
                             f"env_files")
        return cls(spec, p["base"].rstrip("/"), key, model, provider, bool(p.get("local")),
                   cfg.get("temperature"), int(cfg.get("max_tokens", 1200)), cfg.get("reasoning", ""),
                   cfg.get("cache", "auto"), cfg.get("session", ""), cfg.get("prices"), cfg.get("extra"))

    def _wants_breakpoints(self) -> bool:
        if self.cache == "off":
            return False
        if self.cache == "on":
            return True
        return self.provider == "openrouter" and self.model.startswith(BREAKPOINT_MODELS)

    def body(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        if self._wants_breakpoints() and messages and messages[0]["role"] == "system":
            # one breakpoint at the end of the system prompt: the brief, the skills and the rules are the stable prefix
            text = messages[0]["content"]
            messages = [{"role": "system", "content": [{"type": "text", "text": text,
                                                        "cache_control": {"type": "ephemeral"}}]}] + messages[1:]
        body: dict[str, Any] = {"model": self.model, "messages": messages, "max_tokens": self.max_tokens}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if self.provider == "openrouter":
            if self.session:
                body["session_id"] = self.session[:256]
            if self.reasoning:
                body["reasoning"] = {"effort": self.reasoning}
        body.update(self.extra or {})
        return body

    async def chat(self, client: httpx.AsyncClient, messages: list[dict[str, Any]],
                   tools: list[dict[str, Any]]) -> tuple[dict[str, Any], Usage]:
        headers = {"content-type": "application/json"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        if self.provider == "openrouter":
            headers["X-Title"] = "body-harness"
        body = self.body(messages, tools)
        last = ""
        for attempt in range(4):
            t0 = time.monotonic()
            try:
                r = await client.post(f"{self.base}/chat/completions", json=body, headers=headers, timeout=300)
            except httpx.HTTPError as e:
                last = f"{type(e).__name__}: {e}"
                await asyncio.sleep(2 ** attempt)
                continue
            secs = time.monotonic() - t0
            if r.status_code in (429, 500, 502, 503, 504):
                last = f"{r.status_code} {r.text[:300]}"
                await asyncio.sleep(2 ** attempt * 2)
                continue
            if r.status_code != 200:
                raise ModelError(f"{self.spec}: {r.status_code} {r.text[:500]}")
            data = r.json()
            if data.get("error"):
                raise ModelError(f"{self.spec}: {data['error']}")
            choices = data.get("choices") or []
            if not choices:
                raise ModelError(f"{self.spec}: no choices in the answer: {str(data)[:300]}")
            usage = Usage.from_response(data.get("usage"), self.spec, secs, self.prices, self.local)
            return choices[0].get("message") or {}, usage
        raise ModelError(f"{self.spec}: gave up after retries: {last}")


def _key_from_env_files(name: str, files: list[str]) -> str:
    """Read one key from .env files the operator named, never printing it."""
    for f in files:
        p = Path(f).expanduser()
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            k, _, v = line.strip().partition("=")
            if k.strip().removeprefix("export ").strip() == name:
                return v.strip().strip('"').strip("'")
    return ""
