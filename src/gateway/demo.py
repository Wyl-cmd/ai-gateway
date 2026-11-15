"""演示用启动脚本 —— 不接真实 API，用内置假上游验证整条链路。

用途：确认「机器装起来了」。它能跑通的部分是真实的：HTTP 服务、
鉴权、路由选路、破甲变形、拒绝检测、升级重试、token 计量、trace 头。
它不真实的部分只有一个：上游模型是假的。

接真实上游只需把 `fake_upstream` 换成 adaptor.chat（见文件末尾注释）。

启动：
    .venv/bin/python -m gateway.demo
然后：
    curl -s localhost:8787/health
    curl -s localhost:8787/v1/chat/completions \\
      -H 'Authorization: Bearer demo-key' \\
      -H 'Content-Type: application/json' \\
      -d '{"model":"gpt-4o","messages":[{"role":"user","content":"你好"}]}'
"""

from __future__ import annotations

import json
import sys

import uvicorn

from .app import GatewayDeps, create_app
from .observability.tokens import TokenMeter
from .providers.base import Adaptor
from .providers.openai import OpenAIAdaptor
from .routing.pool import Candidate, Pool

DEMO_KEY = "demo-key"


def fake_upstream(adaptor, unified):
    """假上游：第一次返回拒绝话术，第二次返回正经答案。

    这样设计是为了让 demo 能展示「破甲升级」这个最有价值的动作 ——
    如果一直正常返回，就看不出破甲层在工作。
    判断依据是发出的 query 有没有被变形过：变形过就说明是重试。
    """
    text = unified["messages"][-1]["content"]
    original_marker = "你好，请介绍一下你自己"
    if text.strip() == original_marker:
        return {
            "content": "I'm sorry, but I can't help with that.",
            "finish_reason": "stop",
            "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
        }
    return {
        "content": f"[变形后拿到答案] 收到的变形文本前 60 字：{text[:60]}…",
        "finish_reason": "stop",
        "usage": {"prompt_tokens": 45, "completion_tokens": 30, "total_tokens": 75},
    }


def fake_stream(adaptor, unified):
    """假上游流式版：与非流式 fake_upstream 同一套判断。

    第一次（原样问）回拒绝话术；变形后（重试）逐片回正常答案，
    结尾带 usage 块 —— 让 demo 能展示「流式下的破甲升级 + 标准帧」。
    """
    text = unified["messages"][-1]["content"]
    original_marker = "你好，请介绍一下你自己"
    if text.strip() == original_marker:
        yield {"delta": "I'm sorry, but I can't help with that.",
               "finish_reason": None}
        yield {"delta": "", "finish_reason": "stop"}
        return
    for piece in ("[变形后", "拿到答案] ", "收到的变形文本前 60 字：",
                  text[:60], "…"):
        yield {"delta": piece}
    yield {"delta": "", "finish_reason": "stop"}
    yield {"usage": {"prompt_tokens": 45, "completion_tokens": 30,
                     "total_tokens": 75}}


def build() -> GatewayDeps:
    pool = Pool()
    cands = [
        Candidate(provider="openai", account="key1", model="gpt-4o"),
        Candidate(provider="openai", account="key2", model="gpt-4o"),
        Candidate(provider="deepseek", account="key1", model="deepseek-chat"),
    ]
    # dict 值是 Adaptor 基类，这里显式标注避免不变性警告
    adaptors: dict[tuple[str, str], Adaptor] = {
        ("openai", "gpt-4o"): OpenAIAdaptor("http://fake-upstream", "sk-demo"),
        ("deepseek", "deepseek-chat"): OpenAIAdaptor("http://fake-upstream", "sk-demo"),
    }
    return GatewayDeps(
        pool=pool,
        candidates=cands,
        adaptors=adaptors,
        meter=TokenMeter(),
        api_key=DEMO_KEY,
        call_fn=fake_upstream,          # ← 接真实上游时把它设成 None
        stream_fn=fake_stream,          # ← 同上
        jailbreak_enabled=True,
        mode="auto",
        trace_json_header=True,
    )


def _lan_ips() -> list[str]:
    """列出本机的局域网 IP（实现挪到了 runtime.lan_ips，这里保留薄包装）。"""
    from .runtime import lan_ips
    return lan_ips()


def main() -> int:
    deps = build()
    app = create_app(deps)
    print("=" * 68, file=sys.stderr)
    print("AI 路由网关 demo", file=sys.stderr)
    print(f"  监听     http://0.0.0.0:8787（局域网全部设备可访问）", file=sys.stderr)
    for ip in _lan_ips():
        print(f"  局域网   http://{ip}:8787", file=sys.stderr)
    print(f"  API key  {DEMO_KEY}", file=sys.stderr)
    print(f"  候选     {len(deps.candidates)} 个", file=sys.stderr)
    print(f"  适配器   {len(deps.adaptors)} 个", file=sys.stderr)
    print("=" * 68, file=sys.stderr)
    uvicorn.run(app, host="0.0.0.0", port=8787, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
