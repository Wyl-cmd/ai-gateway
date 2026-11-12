"""转发层：5 家 provider 的协议适配 + 统一错误分类。"""

from .base import (
    Adaptor,
    ProviderError,
    classify_failure,
    error_from_response,
    is_health_failure,
    is_quota_exhausted,
)
from .aggregator import Aggregator
from .anthropic import AnthropicAdaptor
from .deepseek import DeepSeekAdaptor
from .gemini import GeminiAdaptor
from .openai import OpenAIAdaptor

__all__ = [
    "Adaptor", "ProviderError", "classify_failure", "error_from_response",
    "is_health_failure", "is_quota_exhausted", "Aggregator",
    "OpenAIAdaptor", "AnthropicAdaptor", "DeepSeekAdaptor", "GeminiAdaptor",
]
