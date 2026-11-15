"""GUI 状态 —— 纯数据 + 格式化，不依赖 Qt。

拆出来的理由：GUI 最容易变成「只能手点、无法测试」的一坨。
把状态和格式化放在这里，就能跑 TDD；Qt 只负责把这些字符串画到屏幕上。

## 用户最需要看见的是什么

不是 token 数、不是延迟分布，是这三件：
  ① 现在是 AUTO 还是 MANUAL
  ② 实际用的哪个型号、哪家供应商
  ③ **降级链** —— 我想用 gpt-4o，它给了 deepseek，中间跳过了谁、为什么

第 ③ 件是这套网关的核心价值。没有它，「auto 模式」就是个黑盒，
用户拿着一张 gpt-4o 的 prompt 收到 deepseek 的答案而不自知。
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ═══════════════════════════════════════════════════════════════
# 单条路由的展示数据
# ═══════════════════════════════════════════════════════════════
@dataclass
class RouteDisplay:
    """一次请求的路由结果，供 GUI 展示。"""

    mode: str                       # auto / manual
    requested: str                  # 用户要的型号
    actual: str                     # 实际用的型号
    provider: str                   # 实际用的供应商
    source: str = ""                # auto_route / manual_strict / manual_fallback
    chain: list[tuple[str, str]] = field(default_factory=list)   # (候选, 原因)
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def model_changed(self) -> bool:
        """实际型号和要的是否不同。

        **换供应商但型号不变不算变** —— 参数语义完全一致，用户要的东西没变。
        只有型号真的变了才该告警。
        """
        return bool(self.actual) and self.actual != self.requested

    @property
    def warning(self) -> str:
        if self.model_changed:
            return f"型号已降级：{self.requested} → {self.actual}"
        return ""

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


# ═══════════════════════════════════════════════════════════════
# 账号行（GUI 表格的数据源）
# ═══════════════════════════════════════════════════════════════
@dataclass
class AccountRow:
    """账号列表里的一行。"""

    provider: str
    account: str
    model: str
    cooled: bool = False            # provider 级冷却
    quota_exhausted: bool = False   # 账号级退避
    uses: int = 0

    @property
    def status_text(self) -> str:
        """状态标签。

        冷却优先于配额 —— 冷却意味着整家 provider 都不能用，
        那是更严重的问题，用户该先看到它。
        """
        if self.cooled:
            return "▲ 冷却中"
        if self.quota_exhausted:
            return "✕ 配额耗尽"
        return "● 正常"


# ═══════════════════════════════════════════════════════════════
# 全局状态
# ═══════════════════════════════════════════════════════════════
class GatewayState:
    """GUI 持有的全部状态。

    **历史有上限**：长跑一天不设限会吃掉几百 MB ——
    GUI 只需展示最近的，完整日志在 request_log 表里。
    """

    def __init__(self, max_history: int = 50) -> None:
        self.mode: str = "auto"
        self.history: list[RouteDisplay] = []
        self.max_history = max_history
        self.total_calls = 0
        self.total_tokens = 0
        self.downgrade_count = 0
        self.accounts: list[AccountRow] = []

    # ── 模式 ──
    def set_mode(self, mode: str) -> None:
        if mode not in ("auto", "manual"):
            raise ValueError(f"mode 只能是 auto 或 manual，收到 {mode!r}")
        self.mode = mode

    # ── 历史 ──
    def push_route(self, d: RouteDisplay) -> None:
        self.history.append(d)
        if len(self.history) > self.max_history:
            # 从头部丢，保留最近的
            del self.history[: len(self.history) - self.max_history]
        self.total_calls += 1
        self.total_tokens += d.total_tokens
        if d.model_changed:
            self.downgrade_count += 1

    @property
    def last_route(self) -> RouteDisplay | None:
        return self.history[-1] if self.history else None


# ═══════════════════════════════════════════════════════════════
# 格式化
# ═══════════════════════════════════════════════════════════════
def format_short_status(d: RouteDisplay) -> str:
    """托盘 tooltip / 状态栏用的一行。"""
    mode = d.mode.upper()
    parts = [mode]
    if d.actual:
        parts.append(d.actual)
    if d.provider:
        parts.append(f"@{d.provider}")
    if d.model_changed:
        parts.append("⚠降级")
    return " · ".join(parts)


def format_route_line(d: RouteDisplay) -> list[str]:
    """降级链，一行一个候选。

    这是 GUI 里最该显眼的一块 —— 用户要能一眼看出
    「我想用 A，为什么最后用了 C」。
    """
    if not d.chain:
        if d.actual:
            return [f"{d.provider}/{d.actual}  ← 直接命中"]
        return []
    out = []
    for cand, reason in d.chain:
        mark = "→" if reason == "入选" else "×"
        out.append(f"{mark} {cand}    {reason}")
    return out


# ═══════════════════════════════════════════════════════════════
# 接线用纯函数（无 Qt，可 TDD）
# ═══════════════════════════════════════════════════════════════
def account_rows(candidates, pool) -> list[AccountRow]:
    """候选池 + Pool 实时状态 → GUI 账号表行。

    供 MainWindow 的轮询 QTimer 调用。Pool 的 state() 有 setdefault
    副作用（会建空记录），但只影响内存里多几条零值记录，无碍。
    """
    return [
        AccountRow(
            provider=c.provider, account=c.account, model=c.model,
            cooled=pool.is_cooled(c.provider),
            quota_exhausted=pool.is_account_backoff(c.provider, c.account),
            uses=pool.state(c).uses,
        )
        for c in candidates
    ]


def route_display_from_trace(d: dict) -> RouteDisplay:
    """app 层 on_request_done 回调的 trace dict → RouteDisplay。

    dict 的键来自 RouteTrace.to_dict()（observability/trace.py），
    usage/jailbreak_level 是接入层回调时附加的。纯映射、无副作用 ——
    GUI 层只管画，不解析。
    """
    usage = d.get("usage") or {}
    return RouteDisplay(
        mode=d.get("mode") or "auto",
        requested=d.get("requested") or "",
        actual=d.get("actual_model") or "",
        provider=d.get("actual_provider") or "",
        source=d.get("source") or "",
        chain=[(c.get("candidate", ""), c.get("reason", ""))
               for c in d.get("candidates", [])],
        prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
        completion_tokens=int(usage.get("completion_tokens", 0) or 0),
    )


def endpoint_lines(host: str, port: int) -> list[tuple[str, str, str]]:
    """主页「接口端点」卡的数据 —— 多协议同时在线，无需切换。

    返回 (协议名, 完整 URL, 鉴权方式)。监听 0.0.0.0 时展示为 127.0.0.1
    （0.0.0.0 是「所有网卡」，不是能敲进客户端的地址）。
    """
    base = f"http://{host if host != '0.0.0.0' else '127.0.0.1'}:{port}"
    return [
        ("OpenAI 协议", f"{base}/v1/chat/completions",
         "Authorization: Bearer <网关密钥>"),
        ("Anthropic 协议", f"{base}/v1/messages",
         "x-api-key: <网关密钥>"),
        ("模型清单", f"{base}/v1/models", "GET"),
    ]
