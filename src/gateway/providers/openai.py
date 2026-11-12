"""OpenAI 适配器 —— 基准格式，其他家都往这个格式对齐。"""

from __future__ import annotations

from .base import Adaptor


class OpenAIAdaptor(Adaptor):
    """OpenAI Chat Completions 协议。

    网关内部统一格式就用它的字段名（messages / temperature / max_tokens），
    所以 transform_request 几乎是恒等变换 —— 唯一要做的是剔掉网关自己的
    私有字段（`_route` / `_trace` 之类，不能让它们漏到上游）。
    """

    name = "openai"

    def build_url(self, model: str, stream: bool) -> str:
        return f"{self.base_url}/chat/completions"

    def build_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def transform_request(self, unified: dict) -> dict:
        # 只放行上游认识的字段；下划线开头的是网关私有，不能漏出去
        skip = {"_route", "_trace", "_session", "_retry"}
        out = {k: v for k, v in unified.items() if not k.startswith("_") and k not in skip}
        return out

    def transform_response(self, raw: dict) -> dict:
        choices = raw.get("choices") or []
        c0 = choices[0] if choices else {}
        msg = c0.get("message") or {}
        usage = raw.get("usage") or {}
        return {
            "content": msg.get("content") or "",
            # 思考模型（deepseek-flash / o1 / qwq 等）的思考过程在 reasoning_content。
            # 不取它会出两个问题：① 用户看不到模型想了什么
            # ② max_tokens 小的时候 content 为空，被上层误判成「拒绝」
            "reasoning": msg.get("reasoning_content") or msg.get("reasoning") or "",
            "finish_reason": c0.get("finish_reason"),
            "usage": {
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
                # 思考 token 单列，方便排查「为什么 max_tokens 不够」
                "reasoning_tokens": (usage.get("completion_tokens_details") or {})
                                    .get("reasoning_tokens", 0),
            },
        }
