"""LM Studio client (OpenAI-compatible, localhost). The model narrates;
it never computes. Numbers are validated before anything is shown."""

from __future__ import annotations

import json
from typing import Any

import httpx

NARRATIVE_SCHEMA = {
    "name": "morning_brief",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "narrative": {"type": "string"},
            "actions": {
                "type": "array", "minItems": 3, "maxItems": 3,
                "items": {"type": "object",
                          "properties": {"text": {"type": "string"},
                                         "category": {"type": "string"}},
                          "required": ["text", "category"], "additionalProperties": False},
            },
            "flags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["narrative", "actions", "flags"],
        "additionalProperties": False,
    },
}

ANSWER_SCHEMA = {
    "name": "chat_answer",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "answer": {"type": "string"},
            "citations": {"type": "array", "items": {"type": "object", "properties": {
                "metric": {"type": "string"}, "value": {"type": "string"},
                "date_range": {"type": "string"}, "device": {"type": "string"},
                "confidence": {"type": "string"}},
                "required": ["metric", "value", "date_range", "device", "confidence"],
                "additionalProperties": False}},
            "caveats": {"type": "array", "items": {"type": "string"}},
            "followups": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["answer", "citations", "caveats", "followups"],
        "additionalProperties": False,
    },
}

SYSTEM_GUARDRAILS = (
    "You are Helios, a private wellness summarizer, not a clinician. Never diagnose, "
    "never name diseases as conclusions, never give medication or supplement dosing. "
    "Use only numbers present verbatim in the provided data; if a metric is missing say "
    "it is unavailable. Frame suggestions as habits, not treatment. Every number you "
    "mention must come from the data provided. Be concise, warm, and honest about "
    "uncertainty. Always mention which device a number came from when citing it."
)


class LMStudio:
    def __init__(self, cfg: dict):
        self.base = cfg["base_url"].rstrip("/")
        self.primary = cfg["primary_model"]
        self.fallback = cfg["fallback_model"]
        self.timeout = float(cfg.get("timeout_seconds", 120))

    def available(self) -> bool:
        try:
            r = httpx.get(f"{self.base}/models", timeout=5)
            return r.status_code == 200
        except Exception:
            return False

    def chat(self, messages: list[dict], model: str | None = None, temperature: float = 0.3,
             response_schema: dict | None = None, tools: list[dict] | None = None,
             ttl: int = 900) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model or self.primary,
            "messages": messages,
            "temperature": temperature,
            "ttl": ttl,
        }
        if response_schema:
            body["response_format"] = {"type": "json_schema", "json_schema": response_schema}
        if tools:
            body["tools"] = tools
        r = httpx.post(f"{self.base}/chat/completions", json=body, timeout=self.timeout)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]

    def structured(self, messages: list[dict], schema: dict, model: str | None = None,
                   temperature: float = 0.2) -> dict:
        msg = self.chat(messages, model=model, temperature=temperature, response_schema=schema)
        # 2026-08-18: reasoning models (qwen3.8-27b) start replies inside a
        # thinking block. With strict json_schema the grammar forces the JSON
        # out immediately, the closing think tag never appears, and LM Studio
        # routes the ENTIRE reply to reasoning_content, leaving content empty.
        # The old line returned {} in that case and the brief retried forever.
        # Trust content first, fall back to reasoning_content, then salvage
        # the first {...} span if either is wrapped in prose.
        raw = (msg.get("content") or "").strip()
        if not raw:
            raw = (msg.get("reasoning_content") or "").strip()
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            start, end = raw.find("{"), raw.rfind("}")
            if start != -1 and end > start:
                try:
                    return json.loads(raw[start:end + 1])
                except json.JSONDecodeError:
                    return {}
            return {}
