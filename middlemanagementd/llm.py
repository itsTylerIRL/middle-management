"""Minimal async client for OpenAI-compatible chat endpoints (llama.cpp server, etc.)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import AsyncIterator
from typing import Any

import aiohttp

from .config import LLMConfig
from .logs import event

log = logging.getLogger(__name__)

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


class LLMError(RuntimeError):
    pass


class _NonRetryable(LLMError):
    pass


class LLMClient:
    def __init__(self, cfg: LLMConfig) -> None:
        self.cfg = cfg
        self._session: aiohttp.ClientSession | None = None

    @property
    def enabled(self) -> bool:
        return self.cfg.enabled

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if key := os.environ.get(self.cfg.api_key_env, ""):
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(
                total=self.cfg.timeout_seconds, sock_connect=self.cfg.connect_timeout_seconds
            )
            self._session = aiohttp.ClientSession(timeout=timeout, headers=self._headers())
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _payload(self, messages: list[dict[str, str]], max_tokens: int, temperature: float,
                 json_mode: bool, stream: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": stream,
        }
        if json_mode and self.cfg.json_mode:
            payload["response_format"] = {"type": "json_object"}
        if self.cfg.disable_thinking:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        return payload

    @property
    def _url(self) -> str:
        return self.cfg.base_url.rstrip("/") + "/chat/completions"

    async def chat(self, messages: list[dict[str, str]], *, max_tokens: int, temperature: float | None = None,
                   json_mode: bool = False) -> str:
        if not self.cfg.enabled:
            raise LLMError("LLM disabled in config")
        payload = self._payload(messages, max_tokens, self._temp(temperature), json_mode, stream=False)
        last: Exception | None = None
        for attempt in range(self.cfg.max_retries + 1):
            try:
                async with self._get_session().post(self._url, json=payload) as resp:
                    if resp.status >= 400:
                        body = (await resp.text())[:200]
                        # Client errors (other than rate limits) won't get better with retries.
                        err = LLMError if resp.status >= 500 or resp.status == 429 else _NonRetryable
                        raise err(f"HTTP {resp.status}: {body}")
                    data = await resp.json(content_type=None)
                content = data["choices"][0]["message"].get("content") or ""
                return _THINK_RE.sub("", content).strip()
            except (aiohttp.ClientError, asyncio.TimeoutError, LLMError, KeyError, ValueError) as e:
                last = e
                event(log, "llm request failed", logging.WARNING, attempt=attempt + 1, error=repr(e)[:200])
                if isinstance(e, _NonRetryable) or attempt >= self.cfg.max_retries:
                    break
                await asyncio.sleep(self.cfg.retry_backoff_seconds * (2**attempt))
        raise LLMError(f"LLM request failed: {last!r}")

    async def stream(self, messages: list[dict[str, str]], *, max_tokens: int,
                     temperature: float | None = None) -> AsyncIterator[str]:
        """Yield content deltas. Retries only before the first token arrives."""
        if not self.cfg.enabled:
            raise LLMError("LLM disabled in config")
        payload = self._payload(messages, max_tokens, self._temp(temperature), False, stream=True)
        for attempt in range(self.cfg.max_retries + 1):
            started = False
            try:
                async with self._get_session().post(self._url, json=payload) as resp:
                    if resp.status >= 400:
                        raise LLMError(f"HTTP {resp.status}: {(await resp.text())[:200]}")
                    in_think = False
                    async for raw in resp.content:
                        line = raw.decode("utf-8", "replace").strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            return
                        try:
                            delta = json.loads(data)["choices"][0].get("delta", {}).get("content") or ""
                        except (ValueError, KeyError, IndexError):
                            continue
                        # Drop any reasoning block the template emitted anyway.
                        if "<think>" in delta:
                            in_think = True
                        if in_think:
                            if "</think>" in delta:
                                in_think = False
                                delta = delta.split("</think>", 1)[1]
                            else:
                                continue
                        if delta:
                            started = True
                            yield delta
                    return
            except (aiohttp.ClientError, asyncio.TimeoutError, LLMError) as e:
                event(log, "llm stream failed", logging.WARNING, attempt=attempt + 1, error=repr(e)[:200])
                if started or attempt >= self.cfg.max_retries:
                    raise LLMError(f"LLM stream failed: {e!r}") from e
                await asyncio.sleep(self.cfg.retry_backoff_seconds * (2**attempt))

    async def health(self) -> dict[str, Any]:
        try:
            url = self.cfg.base_url.rstrip("/") + "/models"
            # Leave room for a slow first mDNS lookup of *.local hosts.
            timeout = aiohttp.ClientTimeout(total=self.cfg.connect_timeout_seconds + 5)
            async with self._get_session().get(url, timeout=timeout) as resp:
                data = await resp.json(content_type=None)
            models = [m.get("id") or m.get("name") for m in data.get("data", data.get("models", []))]
            return {"ok": True, "models": models}
        except Exception as e:  # noqa: BLE001 - health check reports anything
            return {"ok": False, "error": repr(e)[:200]}

    def _temp(self, t: float | None) -> float:
        return self.cfg.temperature if t is None else t


def parse_json_object(text: str) -> dict[str, Any]:
    """Parse a JSON object, tolerating code fences or chatter around it."""
    text = _THINK_RE.sub("", text).strip()
    try:
        obj = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("no JSON object in response") from None
        obj = json.loads(text[start : end + 1])
    if not isinstance(obj, dict):
        raise ValueError("JSON response is not an object")
    return obj


_SENTENCE_END = re.compile(r"([.!?…]+[\"')\]]*)(\s+)")


async def sentences(chunks: AsyncIterator[str], max_chars: int = 240) -> AsyncIterator[str]:
    """Regroup a token stream into speakable sentences."""
    buf = ""
    async for chunk in chunks:
        buf += chunk
        while True:
            m = _SENTENCE_END.search(buf)
            if m:
                cut = m.end(1)
                sentence, buf = buf[:cut].strip(), buf[m.end():]
                if sentence:
                    yield sentence
                continue
            if len(buf) > max_chars:
                cut = buf.rfind(",", 0, max_chars)
                cut = cut + 1 if cut > 40 else max_chars
                sentence, buf = buf[:cut].strip(), buf[cut:]
                yield sentence
                continue
            break
    if buf.strip():
        yield buf.strip()
