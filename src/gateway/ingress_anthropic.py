"""Anthropic 协议接入层 —— /v1/messages 的请求/响应/流式事件翻译。

## 定性（照抄 refs/new-api 的方法）

URL 决定格式（无 body 嗅探）：`/v1/messages` = Claude 原生格式，
`/v1/chat/completions` = OpenAI 格式，**两协议同时在线**，客户端按自己
的 SDK 习惯选端点，网关内部统一格式不变。

形状抄写来源（refs/new-api，能抄不写）：
  请求/响应 dto    relaykit/dto/claude.go（ClaudeRequest / ClaudeResponse）
  请求转换         relaykit/relayconvert/internal/claude_messages/to_oai_chat_req.go
  响应转换         relaykit/relayconvert/internal/oai_chat/to_claude_messages_resp.go
  流式事件序       StreamResponseOpenAI2Claude（message_start → content_block_*
                   → message_delta → message_stop，每帧 event:+data: 双行）
  stop_reason 映射 relaykit/reasonmap/reasonmap.go（双向表）
  鉴权             middleware/auth.go:528（x-api-key 改写成 Bearer，两者等价）

## 诚实边界

tool_use / tool_result / image 等非文本块：**明确报错不支持**（Claude Code
这类重工具客户端暂不可接）——不静默吞块，那是伪兼容。
"""

from __future__ import annotations

import json
from typing import Any, Iterator

__all__ = [
    "parse_messages_request",
    "to_anthropic_response",
    "stream_events",
    "error_body",
]

#: OpenAI finish_reason → Claude stop_reason（refs/new-api relaykit/reasonmap）
_STOP_REASON_MAP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "refusal",
    "stop_sequence": "stop_sequence",
}

#: HTTP 状态码 → Claude 错误类型（正版语义）
_ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    413: "request_too_large",
    429: "rate_limit_error",
}


def _blocks_text(content: Any, where: str) -> str:
    """content 块数组 → 纯文本拼接。非 text 块明确报错（诚实边界）。"""
    parts = []
    for b in content:
        if not isinstance(b, dict):
            raise ValueError(f"{where} 里出现了不是对象的块")
        btype = b.get("type")
        if btype == "text":
            parts.append(str(b.get("text", "")))
        else:
            raise ValueError(
                f"暂不支持 {btype} 块（当前只支持纯文本对话）——"
                f"请用 text 块或换 OpenAI 协议端点 /v1/chat/completions")
    return "\n".join(p for p in parts if p)


def parse_messages_request(body: dict) -> dict:
    """Claude /v1/messages 请求体 → 内部统一格式（OpenAI 形 messages）。

    校验与正版一致：model 必填、messages 必填非空、max_tokens 必填
    （正版缺 max_tokens 返回 invalid_request_error）。
    """
    model = str(body.get("model") or "")
    if not model:
        raise ValueError("缺 model")
    messages_in = body.get("messages")
    if not isinstance(messages_in, list) or not messages_in:
        raise ValueError("messages 不能为空")

    max_tokens = body.get("max_tokens")
    if not max_tokens:
        raise ValueError("缺 max_tokens（Anthropic 协议必填，正版同样要求）")

    out: dict[str, Any] = {"model": model, "messages": [], "max_tokens": max_tokens}

    # system：顶层字段（string 或 block 数组）→ 开头的 system message
    system = body.get("system")
    if system:
        if isinstance(system, str):
            out["messages"].append({"role": "system", "content": system})
        else:
            out["messages"].append({"role": "system",
                                    "content": _blocks_text(system, "system")})

    for m in messages_in:
        role = m.get("role")
        if role not in ("user", "assistant"):
            raise ValueError(f"messages 里出现了不支持的 role: {role!r}")
        content = m.get("content")
        if isinstance(content, str):
            text = content
        else:
            text = _blocks_text(content or [], f"messages[{role}]")
        out["messages"].append({"role": role, "content": text})

    if body.get("stop_sequences"):
        out["stop"] = list(body["stop_sequences"])
    if body.get("temperature") is not None:
        out["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        out["top_p"] = body["top_p"]
    out["stream"] = bool(body.get("stream"))
    return out


def to_anthropic_response(payload: dict) -> dict:
    """内部 OpenAI 壳响应 → Claude message 壳（非流式）。

    照 new-api 的 ResponseOpenAI2Claude：reasoning → thinking block、
    文本 → text block、stop_reason 走 reasonmap、usage 转 input/output_tokens。
    """
    choices = payload.get("choices") or []
    c0 = choices[0] if choices else {}
    msg = c0.get("message") or {}
    usage = payload.get("usage") or {}

    content: list[dict] = []
    reasoning = msg.get("reasoning_content") or msg.get("reasoning")
    if reasoning:
        content.append({"type": "thinking", "thinking": reasoning})
    text = msg.get("content") or ""
    content.append({"type": "text", "text": text})

    fr = c0.get("finish_reason")
    return {
        "id": payload.get("id") or "msg_gateway",
        "type": "message",
        "role": "assistant",
        "model": payload.get("model") or "",
        "content": content,
        "stop_reason": _STOP_REASON_MAP.get(fr, "end_turn"),
        "stop_sequence": None,
        "usage": {
            "input_tokens": int(usage.get("prompt_tokens", 0) or 0),
            "output_tokens": int(usage.get("completion_tokens", 0) or 0),
        },
    }


def _frame(etype: str, data: dict) -> tuple[str, str]:
    """一个 SSE 帧：(event 名, data JSON)。发送方负责拼 event:/data: 双行。"""
    return etype, json.dumps(data, ensure_ascii=False)


def stream_events(
    *,
    deltas: list[str],
    model: str,
    msg_id: str,
    finish_reason: str | None,
    usage: dict | None,
) -> Iterator[tuple[str, str]]:
    """Claude 流式事件序（照 new-api StreamResponseOpenAI2Claude 的形状）。

    message_start（usage.input_tokens）→ content_block_start →
    content_block_delta × N → content_block_stop → message_delta
    （stop_reason + usage.output_tokens）→ message_stop。
    上游没给 usage 时用 0 占位，字段不缺（客户端 SDK 直接可用）。
    """
    input_tokens = int((usage or {}).get("prompt_tokens", 0) or 0)
    output_tokens = int((usage or {}).get("completion_tokens", 0) or 0)

    yield _frame("message_start", {
        "type": "message_start",
        "message": {
            "id": msg_id, "type": "message", "role": "assistant",
            "model": model, "content": [],
            "usage": {"input_tokens": input_tokens, "output_tokens": 0},
        },
    })
    yield _frame("content_block_start", {
        "type": "content_block_start", "index": 0,
        "content_block": {"type": "text", "text": ""},
    })
    for d in deltas:
        if d:
            yield _frame("content_block_delta", {
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": d},
            })
    yield _frame("content_block_stop", {"type": "content_block_stop", "index": 0})
    yield _frame("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": _STOP_REASON_MAP.get(finish_reason, "end_turn"),
                  "stop_sequence": None},
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    })
    yield _frame("message_stop", {"type": "message_stop"})


def error_body(status: int, message: str) -> dict:
    """Claude 错误壳（正版语义：{"type":"error","error":{type,message}}）。"""
    etype = _ERROR_TYPES.get(status, "api_error")
    return {"type": "error", "error": {"type": etype, "message": message}}
