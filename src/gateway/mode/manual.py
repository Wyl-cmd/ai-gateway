"""G7 手动模式 —— 三档寻址解析。

三档的写法就是「越写越具体」：

    gpt-4o               只要这个型号，哪家哪号都行
    openai/gpt-4o        这个型号，而且必须是 openai 的
    openai:key2/gpt-4o   这个型号，openai 的，而且必须是 key2 这个账号

**为什么不做「只锁 provider 不写型号」**：没有型号的 provider 锁没有意义 ——
用户点 provider 是想「用这家的某个模型」，不是「用这家的任意模型」。
真要做，调用方写 `openai/*` 更清楚，但那属于 G8 接入层的事，不在解析这一层。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class TargetLock(StrEnum):
    """锁定粒度。"""

    MODEL_ONLY = "model_only"      # 只锁型号
    PROVIDER = "provider"          # 锁型号 + provider
    ACCOUNT = "account"            # 锁型号 + provider + 账号


class FallbackPolicy(StrEnum):
    """降级策略。

    STRICT 是默认值，理由见 ModeConfig 的 docstring。
    """

    STRICT = "strict"
    SAME_MODEL = "fallback_same_model"
    ANY_MODEL = "fallback_any_model"


@dataclass(frozen=True)
class ManualTarget:
    model: str
    provider: str | None = None
    account: str | None = None
    lock: TargetLock = TargetLock.MODEL_ONLY

    def __str__(self) -> str:
        if self.lock == TargetLock.ACCOUNT:
            return f"{self.provider}:{self.account}/{self.model}"
        if self.lock == TargetLock.PROVIDER:
            return f"{self.provider}/{self.model}"
        return self.model


#: 内置的主流 provider 名。用来消解「斜杠到底是分隔符还是型号名的一部分」。
#:
#: 没有这个列表就无解 —— `openai/gpt-4o` 和 `meta-llama/Llama-3-70B`
#: 结构完全一样（左边斜杠右边），但前者该切、后者不该切。
#: 唯一的区别是「左边这个名字认不认识」。
DEFAULT_PROVIDERS: frozenset[str] = frozenset({
    # 海外
    "openai", "azure", "anthropic", "google", "gemini", "vertex", "bedrock",
    "aws", "mistral", "cohere", "xai", "groq", "together", "fireworks",
    "openrouter", "perplexity", "replicate", "huggingface", "deepinfra",
    "hyperbolic", "novita", "sambanova", "cerebras", "vercel",
    # 国内
    "deepseek", "moonshot", "kimi", "qwen", "dashscope", "aliyun",
    "zhipu", "glm", "baichuan", "minimax", "yi", "lingyiwanwu",
    "stepfun", "doubao", "volcengine", "baidu", "ernie", "hunyuan",
    "tencent", "sensetime", "sensenova", "siliconflow", "infini",
})


def parse_target(spec: str, *, known_providers: set[str] | None = None) -> ManualTarget:
    """解析手动模式的目标声明。

    **斜杠的歧义靠「认不认识左边这个名字」来消解**：
      `openai/gpt-4o`            → openai 是已知 provider → 切
      `openai:key2/gpt-4o`       → 有冒号，明确是「provider:账号」 → 切
      `meta-llama/Llama-3-70B`   → meta-llama 不是 provider 名 → 整串当型号

    不给 `known_providers` 时用内置的 `DEFAULT_PROVIDERS`。
    硬编码「斜杠左边就是 provider」会让 OpenRouter / Together 那类以
    `组织/模型` 命名的上游全部解析错。
    """
    if not spec or not spec.strip():
        raise ValueError("目标声明不能为空")
    spec = spec.strip()

    known = known_providers if known_providers is not None else set(DEFAULT_PROVIDERS)

    provider = account = None
    model = spec

    if "/" in spec:
        left, right = spec.split("/", 1)
        if not right:
            raise ValueError(f"目标声明缺型号: {spec!r}")

        # left 可能是 provider，也可能是 provider:account
        prov = left
        acct = None
        if ":" in left:
            prov, acct = left.split(":", 1)
            if not acct:
                raise ValueError(f"账号名为空: {spec!r}")

        # 冒号是明确的「provider:账号」信号；否则看左边认不认识
        if acct is not None or prov in known:
            provider, account, model = prov, acct, right

    if account is not None:
        lock = TargetLock.ACCOUNT
    elif provider is not None:
        lock = TargetLock.PROVIDER
    else:
        lock = TargetLock.MODEL_ONLY

    return ManualTarget(model=model, provider=provider, account=account, lock=lock)
