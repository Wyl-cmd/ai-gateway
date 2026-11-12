"""Google Gemini generateContent 适配器。

差异最大的一家，四处结构全不一样：
  1. `messages`        → `contents`（每个含 parts 数组）
  2. role 名           → assistant 要改成 model，system 不能出现在 contents 里
  3. system            → `systemInstruction.content.parts[].text`
  4. 生成参数           → 全部收进 `generationConfig`（temperature / maxOutputTokens）
  5. 鉴权               → 走 query string `?key=`，不是 header
"""

from __future__ import annotations

from .base import Adaptor


class GeminiAdaptor(Adaptor):
    name = "gemini"

    def build_url(self, model: str, stream: bool) -> str:
        # 鉴权在 query string 里（Google 的设计），stream 也要走不同端点
        action = "streamGenerateContent" if stream else "generateContent"
        return f"{self.base_url}/v1beta/models/{model}:{action}?key={self.api_key}"

    def build_headers(self) -> dict:
        return {"Content-Type": "application/json"}

    def transform_request(self, unified: dict) -> dict:
        msgs = list(unified.get("messages") or [])

        # ① system 抽出来单独放 systemInstruction
        sys_parts = [m.get("content", "") for m in msgs if m.get("role") == "system"]
        msgs = [m for m in msgs if m.get("role") != "system"]

        # ② role 映射：assistant → model，其余保持
        role_map = {"assistant": "model", "user": "user", "tool": "user"}
        contents = []
        for m in msgs:
            contents.append({
                "role": role_map.get(m.get("role", "user"), "user"),
                "parts": [{"text": m.get("content", "")}],
            })

        out = {"contents": contents}

        if sys_parts:
            out["systemInstruction"] = {
                "parts": [{"text": "\n".join(p for p in sys_parts if p)}]
            }

        # ③ 生成参数全收进 generationConfig
        gen = {}
        if unified.get("temperature") is not None:
            gen["temperature"] = unified["temperature"]
        if unified.get("max_tokens") is not None:
            gen["maxOutputTokens"] = unified["max_tokens"]     # 名字不同
        if unified.get("top_p") is not None:
            gen["topP"] = unified["top_p"]
        if unified.get("stop"):
            sp = unified["stop"]
            gen["stopSequences"] = [sp] if isinstance(sp, str) else list(sp)
        if gen:
            out["generationConfig"] = gen

        return out

    def transform_response(self, raw: dict) -> dict:
        cands = raw.get("candidates") or []
        c0 = cands[0] if cands else {}
        parts = (c0.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
        meta = raw.get("usageMetadata") or {}
        pt = meta.get("promptTokenCount", 0)
        ct = meta.get("candidatesTokenCount", 0)
        return {
            "content": text,
            "finish_reason": c0.get("finishReason"),
            "usage": {
                "prompt_tokens": pt,
                "completion_tokens": ct,
                "total_tokens": meta.get("totalTokenCount", pt + ct),
            },
        }

    def transform_delta(self, raw: dict) -> dict:
        """Gemini 流式每块也是完整 candidates 结构。"""
        cands = raw.get("candidates") or []
        if not cands:
            return {}
        c0 = cands[0]
        parts = (c0.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
        return {"delta": text, "finish_reason": c0.get("finishReason")}
