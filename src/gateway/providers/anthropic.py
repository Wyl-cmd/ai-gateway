"""Anthropic Messages 适配器。

两处关键差异（不是「换个 URL」就完事）：
  1. system 不在 messages 里，是顶层独立字段 `system`
  2. `max_tokens` 是必填项，不传直接 400
  3. 响应的 content 是 block 数组，要拼接
  4. usage 字段叫 input_tokens / output_tokens
"""

from __future__ import annotations

from .base import Adaptor


class AnthropicAdaptor(Adaptor):
    name = "anthropic"

    def build_url(self, model: str, stream: bool) -> str:
        return f"{self.base_url}/v1/messages"

    def build_headers(self) -> dict:
        return {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

    def transform_request(self, unified: dict) -> dict:
        msgs = list(unified.get("messages") or [])

        # 把 system 消息抽出来提到顶层（Anthropic 没有 system role）
        system_parts = [m.get("content", "") for m in msgs if m.get("role") == "system"]
        msgs = [m for m in msgs if m.get("role") != "system"]

        out = {
            "model": unified.get("model", ""),
            "messages": msgs,
            # max_tokens 必填 —— 上游默认 4096 太大会烧钱，调用方没给就用 1024
            "max_tokens": unified.get("max_tokens") or 1024,
        }
        if system_parts:
            out["system"] = "\n".join(p for p in system_parts if p)
        if unified.get("temperature") is not None:
            out["temperature"] = unified["temperature"]
        if unified.get("top_p") is not None:
            out["top_p"] = unified["top_p"]
        if unified.get("stop"):
            sp = unified["stop"]
            out["stop_sequences"] = [sp] if isinstance(sp, str) else list(sp)
        return out

    def transform_response(self, raw: dict) -> dict:
        blocks = raw.get("content") or []
        # content 是 block 数组，text 类型的拼起来；thinking 之类的块忽略
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        usage = raw.get("usage") or {}
        pt = usage.get("input_tokens", 0)
        ct = usage.get("output_tokens", 0)
        return {
            "content": text,
            "finish_reason": raw.get("stop_reason"),
            "usage": {
                "prompt_tokens": pt,
                "completion_tokens": ct,
                "total_tokens": pt + ct,
            },
        }

    def transform_delta(self, raw: dict) -> dict:
        """Anthropic 的流式事件类型不同：content_block_delta / message_stop。"""
        t = raw.get("type")
        if t == "content_block_delta":
            d = raw.get("delta") or {}
            return {"delta": d.get("text", "")}
        if t == "message_delta":
            return {"finish_reason": (raw.get("delta") or {}).get("stop_reason")}
        if t == "message_stop":
            return {"done": True}
        return {}
