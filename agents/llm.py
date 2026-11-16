"""The one place that calls the LLM (QUT's Azure OpenAI gateway).

Cost controls:
  - small model (gpt-4.1-mini), short prompts, tight max_completion_tokens
  - temperature 0 + structured JSON / strict tool schemas, so no retries
  - at most 4 calls per case (intake may ask the customer once or twice)
  - every answer is cached in data/llm_cache.json: re-running a case costs $0
    and gives the identical answer (good for the live demo)
  - AGENTLENS_OFFLINE=1 never calls the API; it uses the cache only, and
    callers fall back to plain-Python rules on a cache miss
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from config import REPO_ROOT  # loads .env

AZURE_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT", "https://qut-ai.azure-api.net/ifb220")
AZURE_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2025-03-01-preview")
MODEL = os.getenv("LLM_MODEL", "gpt-4.1-mini")

_client = None


class LLMError(Exception):
    pass


def _cache_path() -> Path:
    return Path(os.getenv("AGENTLENS_LLM_CACHE", REPO_ROOT / "data" / "llm_cache.json"))


def _cache_read() -> dict:
    p = _cache_path()
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _cache_write(key: str, value: dict) -> None:
    cache = _cache_read()
    cache[key] = value
    p = _cache_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cache, indent=1), encoding="utf-8")


def _get_client():
    global _client
    if _client is None:
        from openai import AzureOpenAI

        api_key = (os.getenv("API_KEY") or "").strip()
        if not api_key:
            raise LLMError("API_KEY is not set in .env")
        _client = AzureOpenAI(azure_endpoint=AZURE_ENDPOINT, api_key=api_key,
                              api_version=AZURE_API_VERSION)
    return _client


def chat(messages: list[dict], *, tools: list[dict] | None = None, tool_choice=None,
         schema: dict | None = None, schema_name: str | None = None,
         max_tokens: int = 200) -> tuple[dict, dict]:
    """One cached chat completion. Returns (reply, meta).

    reply = {"content": str | None, "tool_calls": [{"id", "name", "arguments", "arguments_raw"}]}
    meta  = {"model", "cached", "input_tokens", "output_tokens"}. Raises LLMError.
    """
    request = {"messages": messages, "max_completion_tokens": max_tokens, "temperature": 0}
    if tools:
        request |= {"tools": tools, "tool_choice": tool_choice or "auto", "parallel_tool_calls": False}
    if schema:
        request["response_format"] = {"type": "json_schema",
                                      "json_schema": {"name": schema_name, "schema": schema, "strict": True}}
    key = hashlib.sha256(json.dumps([MODEL, request], sort_keys=True).encode()).hexdigest()
    hit = _cache_read().get(key)
    if hit:
        return hit["reply"], {"model": MODEL, "cached": True, "input_tokens": 0, "output_tokens": 0}
    if os.getenv("AGENTLENS_OFFLINE") == "1":
        raise LLMError("AGENTLENS_OFFLINE=1 and no cached answer")

    import openai

    try:
        response = _get_client().chat.completions.create(model=MODEL, **request)
    except openai.AuthenticationError as e:
        raise LLMError("API key rejected (401)") from e
    except openai.RateLimitError as e:
        raise LLMError("rate limited") from e
    except openai.APIStatusError as e:
        raise LLMError(f"API error {e.status_code}") from e
    except openai.APIConnectionError as e:
        raise LLMError("could not reach the LLM gateway") from e

    choice = response.choices[0]
    if choice.message.refusal or choice.finish_reason not in ("stop", "tool_calls"):
        raise LLMError(f"LLM stopped early ({choice.finish_reason})")
    try:
        reply = {
            "content": choice.message.content,
            "tool_calls": [{"id": tc.id, "name": tc.function.name,
                            "arguments": json.loads(tc.function.arguments),
                            "arguments_raw": tc.function.arguments}
                           for tc in (choice.message.tool_calls or [])],
        }
    except json.JSONDecodeError as e:
        raise LLMError("LLM returned invalid tool arguments") from e

    _cache_write(key, {"reply": reply})
    usage = response.usage
    return reply, {"model": response.model or MODEL, "cached": False,
                   "input_tokens": usage.prompt_tokens if usage else None,
                   "output_tokens": usage.completion_tokens if usage else None}


def chat_json(system: str, user: str, schema: dict, schema_name: str,
              max_tokens: int = 200) -> tuple[dict, dict]:
    """Ask for JSON matching `schema`. Returns (data, meta). Raises LLMError."""
    reply, meta = chat([{"role": "system", "content": system}, {"role": "user", "content": user}],
                       schema=schema, schema_name=schema_name, max_tokens=max_tokens)
    try:
        return json.loads(reply["content"] or ""), meta
    except json.JSONDecodeError as e:
        raise LLMError("LLM returned invalid JSON") from e


def assistant_message(reply: dict) -> dict:
    """Turn a reply back into a message to continue the conversation."""
    msg = {"role": "assistant", "content": reply["content"]}
    if reply["tool_calls"]:
        msg["tool_calls"] = [{"id": tc["id"], "type": "function",
                              "function": {"name": tc["name"], "arguments": tc["arguments_raw"]}}
                             for tc in reply["tool_calls"]]
    return msg
