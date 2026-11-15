"""转发层基类 —— Adaptor 抽象契约 + 统一错误分类。

契约抄自 one-api `relay/adaptor/interface.go` 的 Adaptor 接口，
错误分类抄自 freellmpool `router.py` 的 `_is_health_failure` /
`_is_account_quota_exhaustion`（2026-11-11 实测源码，非 README 转述）。

**为什么错误分类是这一层最重要的东西**：
G3 路由层做失败决策时只有这一个输入。分错一次，整个降级链就错：
  - 把 400 当健康故障 → 一个坏请求会把整家 provider 打成故障，后续流量全绕开
  - 把「配额耗尽」当健康故障 → 跳过整个 provider，而不是只跳过一个 key
  - 把「限速 429」当配额耗尽 → 该账号被永久标记废掉，实际等 60 秒就能用
"""

from __future__ import annotations

import abc
import json
from typing import Any, Iterator


# ═══════════════════════════════════════════════════════════════
# 错误
# ═══════════════════════════════════════════════════════════════
# 配额耗尽的关键词（抄 freellmpool，按国内厂商补了几条）
_QUOTA_MARKERS = (
    # 英文
    "depleted your monthly",
    "used up your daily free allocation",
    "insufficient credits",
    "insufficient quota",
    "quota exhausted",
    "quota has been exhausted",
    "credit balance",
    "exceeded your current quota",
    "billing hard limit",
    # 中文（国内厂商常见文案）
    "余额不足",
    "额度已用尽",
    "配额已用尽",
    "账户欠费",
    "已超出套餐",
)


class ProviderError(Exception):
    """上游返回的错误，带两个供路由层决策的布尔标记。

    is_health_failure    —— 算「这家 provider 现在病了」（429/408/5xx/网络）
    is_quota_exhausted   —— 算「这个账号的额度用完了」（402/429 + 配额文案）
    """

    def __init__(
        self,
        message: str,
        status: int | None = None,
        provider: str | None = None,
        raw: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.provider = provider
        self.raw = raw
        self.is_health_failure = _health_failure_of(status)
        self.is_quota_exhausted = _quota_of(message, status, provider)


def _health_failure_of(status: int | None) -> bool:
    """只有可用性类故障才算健康故障。

    429 限速 / 408 超时 / 5xx 服务端错 / status 为 None（网络层异常）→ True
    其余 4xx（400 请求体坏、401/403 鉴权、402 欠费、404 模型不存在）→ False

    **402 特别处理**：它是「这个号没钱了」，不是「这家 provider 挂了」。
    算成健康故障会导致同一 provider 的其他 key 也被跳过 —— 这正是 freellmpool
    注释里警告的错误。
    """
    if status is None:
        return True                      # 连接失败、超时、DNS 错误
    return status in (408, 429) or status >= 500


def _quota_of(message: str, status: int | None, provider: str | None) -> bool:
    """配额耗尽识别。

    只看 402 / 429 —— 其他状态码即使文案里有 "quota" 也不算
    （404 的「未知模型」、400 的「参数超限」都容易误命中）。
    """
    if status not in (402, 429):
        return False
    # Vercel 的 402 一律视为额度耗尽（freellmpool 特例）
    if provider == "vercel" and status == 402:
        return True
    low = message.lower()
    return any(m.lower() in low for m in _QUOTA_MARKERS)


def is_health_failure(exc: Exception) -> bool:
    """外部判据：这个异常是否说明 provider 病了。"""
    if isinstance(exc, ProviderError):
        return exc.is_health_failure
    # 网络层异常（httpx.ConnectError / ReadTimeout 等）都算
    return True


def is_quota_exhausted(exc: Exception) -> bool:
    if isinstance(exc, ProviderError):
        return exc.is_quota_exhausted
    return False


def classify_failure(exc: Exception) -> str:
    """把异常归到唯一一类，供 route_trace 记录。

    返回 "quota" / "health" / "client"，顺序有讲究：
    先判 quota —— 配额耗尽虽然状态码是 429（本该算 health），
    但它的处理方式完全不同（只跳账号，不跳 provider）。
    """
    if is_quota_exhausted(exc):
        return "quota"
    if is_health_failure(exc):
        return "health"
    return "client"


def error_from_response(resp: Any, provider: str | None = None) -> ProviderError:
    """把 httpx.Response 的非 2xx 转成 ProviderError。"""
    status = getattr(resp, "status_code", None)
    try:
        body = resp.text[:1000]
    except Exception:
        body = ""
    # 尽量从 JSON 里挖出人类可读的 message
    msg = body
    try:
        j = resp.json()
        if isinstance(j, dict):
            err = j.get("error")
            if isinstance(err, dict):
                msg = err.get("message") or err.get("type") or body
            elif isinstance(err, str):
                msg = err
            else:
                msg = j.get("message") or j.get("msg") or body
    except Exception:
        pass
    return ProviderError(str(msg), status=status, provider=provider, raw=body)


# ═══════════════════════════════════════════════════════════════
# Adaptor 抽象契约
# ═══════════════════════════════════════════════════════════════
class Adaptor(abc.ABC):
    """一家上游的协议适配器。

    契约对应 one-api 的 Adaptor 接口，但按 Python 的习惯合并成了 5 个方法：
      transform_request   统一格式 → 自家格式
      transform_response  自家格式 → 统一格式
      parse_sse           逐块解析流式（要能扛住被切断的 chunk）
      chat                非流式一次请求
      stream              流式逐块产出
    """

    name: str = ""                       # 子类必须覆盖

    def __init__(self, base_url: str = "", api_key: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._sse_buf = b""              # 流式解析的跨块缓冲

    # ── 纯函数部分（不发请求，测试全打这里）──
    @abc.abstractmethod
    def transform_request(self, unified: dict) -> dict:
        """把网关统一格式转成这家上游要的格式。"""

    @abc.abstractmethod
    def transform_response(self, raw: dict) -> dict:
        """把上游返回转回统一格式：{content, finish_reason, usage{...}}。"""

    @abc.abstractmethod
    def build_url(self, model: str, stream: bool) -> str:
        """拼出这家上游的请求 URL。"""

    @abc.abstractmethod
    def build_headers(self) -> dict:
        """鉴权头。"""

    def parse_sse(self, chunk: bytes) -> list[dict]:
        """解析 SSE 数据块，返回 [{delta, finish_reason}] 列表。

        必须能处理「一块被切成两半」的情况 —— 这是流式最常见的坑：
        `data: {"choices":[{"delta":{"con` + `tent":"hi"}}]}` 两块的边界
        既不在行首也不在 JSON 边界上，只能靠缓冲。

        **缓冲跨调用保留**（不清空）。这是流式解析的正确行为：
        半截数据留在 `_sse_buf` 里，等下一块补上再解析。
        所以 `parse_sse(part1) + parse_sse(part2)` 能拼出完整数据。
        生产环境用 `feed_sse`（同一个函数，保留这个名字是为了语义清晰）。
        """
        return self.feed_sse(chunk)

    def feed_sse(self, chunk: bytes) -> list[dict]:
        """保留缓冲的流式解析（生产用这个）。"""
        self._sse_buf += chunk
        out: list[dict] = []
        while True:
            idx = self._sse_buf.find(b"\n\n")
            if idx < 0:
                break
            block, self._sse_buf = self._sse_buf[:idx], self._sse_buf[idx + 2:]
            out.extend(self._parse_block(block))
        return out

    def _parse_block(self, block: bytes) -> list[dict]:
        """解析单个 SSE 事件块（已按 \\n\\n 切开）。"""
        text = block.decode("utf-8", errors="replace")
        datas = []
        for line in text.split("\n"):
            line = line.strip()
            if not line or line.startswith(":"):
                continue                     # 空行、注释行（`: ping`）跳过
            if line.startswith("data:"):
                datas.append(line[5:].strip())
        out: list[dict] = []
        for d in datas:
            if d == "[DONE]":
                out.append({"done": True})
                continue
            try:
                j = json.loads(d)
            except json.JSONDecodeError:
                continue                     # 解析不了就跳过，不抛（流式里半截数据常见）
            if isinstance(j, dict):
                out.append(self.transform_delta(j))
        return out

    def transform_delta(self, raw: dict) -> dict:
        """把上游单个流式事件转成 {delta, finish_reason}。默认按 OpenAI 格式。

        usage 块（stream_options.include_usage 的最终 chunk，choices 为空）
        不能丢 —— 网关的流式响应要能把这个 usage 转给客户端。
        """
        choices = raw.get("choices") or []
        if not choices:
            usage = raw.get("usage")
            return {"usage": usage} if usage else {}
        c0 = choices[0]
        delta = (c0.get("delta") or {}).get("content") or ""
        out = {"delta": delta, "finish_reason": c0.get("finish_reason")}
        # 有些上游把 usage 附在最后一个内容块上而不是独立块
        if raw.get("usage"):
            out["usage"] = raw["usage"]
        return out

    # ── 发请求部分（G10 端到端才会真打）──
    def chat(self, unified: dict) -> dict:
        import httpx

        payload = self.transform_request(unified)
        url = self.build_url(payload.get("model", ""), stream=False)
        try:
            resp = httpx.post(url, json=payload, headers=self.build_headers(), timeout=120)
        except Exception as e:
            raise ProviderError(f"{type(e).__name__}: {e}", status=None,
                                provider=self.name) from e
        if resp.status_code >= 400:
            raise error_from_response(resp, self.name)
        return self.transform_response(resp.json())

    def stream(self, unified: dict) -> Iterator[dict]:
        import httpx

        payload = self.transform_request(unified)
        payload["stream"] = True
        url = self.build_url(payload.get("model", ""), stream=True)
        try:
            with httpx.stream("POST", url, json=payload,
                              headers=self.build_headers(), timeout=300) as resp:
                if resp.status_code >= 400:
                    raise error_from_response(resp, self.name)
                for chunk in resp.iter_bytes():
                    for ev in self.feed_sse(chunk):
                        yield ev
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"{type(e).__name__}: {e}", status=None,
                                provider=self.name) from e
