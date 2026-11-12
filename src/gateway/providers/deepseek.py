"""DeepSeek 适配器 —— OpenAI 兼容协议，只换 base_url 和少量默认值。"""

from __future__ import annotations

from .openai import OpenAIAdaptor


class DeepSeekAdaptor(OpenAIAdaptor):
    """DeepSeek 的 API 是完全 OpenAI 兼容的。

    **能抄不写**：直接继承 OpenAIAdaptor，只改 name。
    （曾经考虑过单独维护一份 transform，理由是 DeepSeek 有自己的
    `reasoning_content` 字段 —— 但那只是响应里多一个字段，
    统一格式取 `content` 就够了，多维护一份只会漂移。）
    """

    name = "deepseek"
