"""G8 接入层 —— FastAPI 应用，把前面所有零件装成机器。

这是第一个「能真跑」的批次。链路：

    鉴权 → 校验 → 路由选路 → 破甲变形 → 调上游 → 拒绝检测 → 升级重试 → 计量

## 双协议同时在线（定性照抄 refs/new-api）

**URL 决定格式，不做 body 嗅探，不做协议切换开关**：

    /v1/chat/completions   OpenAI 协议（ChatGPT 系客户端）
    /v1/messages           Anthropic 协议（Claude 系客户端）
    /v1/models             模型清单（按鉴权头返回对应协议形状）

两个入口共用同一条管线（路由/破甲/检测/重试），协议只是「壳」：
入口翻译成统一格式，出口翻译回对应协议形状。
形状抄写来源见 ingress_anthropic.py 模块注释（relaykit/dto、reasonmap）。

## 接入层不做任何自己的判断

它只负责接线。一旦它开始「顺手也判一下」，就成了第二个决策点，
和 G3/G4 的判断必然不一致 —— 那时候「为什么走了这条路」再也说不清。
所以这里的每个 if 都是在**照抄上游层的结论**，不是在产生新结论。

## 重试的两层结构

    外层：候选队列（换账号 / 换家 / 换型号）—— G7 决定队列里有什么
    内层：破甲阶梯（L0 → L1 → L2 …）—— G4 决定每级怎么变形

顺序有讲究：**先在同一账号上把破甲试完，再换账号**。
因为换账号换的是「额度」，不换「能力」—— 同一个模型换个 key，
审查策略一模一样，破甲该拒还是拒。先把破甲试够再换号才不浪费。

但反过来，**上游故障（5xx）要直接换账号**，不能耗在破甲上 ——
provider 挂了，变形一百次也还是 404。

## 流式的两条路径（诚实语义）

  破甲开：上游仍流式拉取但**全量缓冲**，完整文本过拒绝检测，
          拒绝 → 按阶梯换技术重试（与非流式完全对称）；
          合格 → 把已收的 delta 按 SSE 标准帧回放给客户端。
          代价：首字延迟 = 完整上游耗时（拒绝检测必须先见全文，不是偷工）。
  破甲关：真·逐 delta 直通（无缓冲无重试，与非流式 jb 关语义一致）。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .ingress_anthropic import (
    error_body as anthropic_error_body,
    parse_messages_request,
    stream_events as anthropic_stream_events,
    to_anthropic_response,
)
from .jailbreak.refusal import detect
from .jailbreak.technique import Level, select_techniques
from .jailbreak.templates import TEMPLATES, render as render_template
from .mode.router import FallbackPolicy, ModeRouter, RouteFailed
from .observability.tokens import TokenMeter
from .observability.trace import RouteTrace, TraceStage
from .providers.base import Adaptor, ProviderError
from .routing.pool import Candidate, Pool, RoutingPolicy


#: 破甲最多试几次。上限存在的意义是防烧钱 —— 一个永远拒的模型
#: 不该被无限重试，用户等 30 秒拿到「还是拒绝」比等 5 分钟好。
MAX_JAILBREAK_ATTEMPTS = 6

#: 调用方没给 max_tokens 时的默认值。
#:
#: **必须给足**：实测 deepseek-flash 这类思考模型，一次普通问答 reasoning 就要
#: 200~400 tokens，破甲变形（Base64/模板）后会到 1000+。给 900 的结果是
#: reasoning 吃光预算、content 为空、finish_reason=length —— 看起来像「被拒绝」，
#: 实际是「没想完就没 token 了」。
DEFAULT_MAX_TOKENS = 8192

#: 流式迭代哨兵（to_thread(next, it, 哨兵) 区分「迭代完」与 None）
_NO_MORE = object()


# ═══════════════════════════════════════════════════════════════
# 依赖容器
# ═══════════════════════════════════════════════════════════════
@dataclass
class GatewayDeps:
    """接入层需要的一切。全部注入，方便测试打桩。"""

    pool: Pool
    candidates: list[Candidate]
    adaptors: dict[tuple[str, str], Adaptor]
    meter: TokenMeter
    api_key: str
    #: 多把网关密钥（runtime 装配从 auth_keys 来）。None/空 = 只认 api_key；
    #: 两者都空 = 不鉴权（与 config「留空 = 仅本机安全」语义一致）
    api_keys: list[str] | None = None
    #: (adaptor, unified) -> dict。None 时走 adaptor.chat()
    call_fn: Callable[[Adaptor, dict], dict] | None = None
    #: (adaptor, unified) -> Iterator[dict]。None 时走 adaptor.stream()
    stream_fn: Callable[[Adaptor, dict], Iterator[dict]] | None = None
    jailbreak_enabled: bool = True
    #: 技术有效性记忆。None 时退回纯先验顺序（select_techniques 的默认行为）
    memory: Any = None
    #: 破甲产物库（跨模型迁移）。None 时不做迁移，只走阶梯
    artifacts: Any = None
    mode: str = "auto"
    target: str | None = None
    policy: str = FallbackPolicy.STRICT.value
    known_providers: set[str] | None = None
    #: 装配好的评分策略。None 时用默认 RoutingPolicy()
    routing_policy: RoutingPolicy | None = None
    trace_json_header: bool = False
    #: 每个请求收尾时回调（GUI 用它填路由链/统计）。失败不影响主链路
    on_request_done: Callable[[dict], None] | None = None


# ═══════════════════════════════════════════════════════════════
# 工具
# ═══════════════════════════════════════════════════════════════
def _err(status: int, etype: str, msg: str, proto: str = "openai") -> JSONResponse:
    """错误响应。OpenAI 与 Anthropic 各自的错误壳（形状照各自正版）。"""
    if proto == "anthropic":
        return JSONResponse(status_code=status, content=anthropic_error_body(status, msg))
    return JSONResponse(status_code=status,
                        content={"error": {"type": etype, "message": msg}})


def _last_user_text(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            return str(m.get("content", ""))
    return ""


def _replace_last_user(messages: list[dict], new_text: str) -> list[dict]:
    out = list(messages)
    for i in range(len(out) - 1, -1, -1):
        if out[i].get("role") == "user":
            out[i] = {**out[i], "content": new_text}
            break
    return out


def _pick_adaptor(deps: GatewayDeps, c: Candidate) -> Adaptor:
    """按候选找适配器。查找顺序（两级都是 (provider, x) 二元组）：

        ① (provider, account) —— runtime 装配的键：一账号一实例一 key
        ② (provider, model)   —— demo / 旧测试的键
        ③ provider 兜底        —— 同家任意实例（key 不区分账号的场景）

    ①先于②是**正确性**问题：同 provider 多账号时，(provider, model)
    命中的是「先注册的那个账号」的 key，账号轮巡就会全打到一把 key 上。
    """
    a = deps.adaptors.get((c.provider, c.account))
    if a is not None:
        return a
    a = deps.adaptors.get((c.provider, c.model))
    if a is not None:
        return a
    for (prov, _), ad in deps.adaptors.items():
        if prov == c.provider:
            return ad
    raise ProviderError(f"没有 {c.provider}/{c.model} 的适配器", status=None,
                        provider=c.provider)


def _to_openai_shape(content: str, finish_reason: str | None, model: str) -> dict:
    return {
        "id": f"chatcmpl-gw-{int(time.time()*1000)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": finish_reason or "stop",
        }],
    }


# ═══════════════════════════════════════════════════════════════
# 破甲阶梯：给第 n 次尝试生成变形后的 query
# ═══════════════════════════════════════════════════════════════
def _jailbreak_text(original: str, attempt: int,
                    model: str = "", memory: Any = None,
                    artifacts: Any = None) -> tuple[str, int, str]:
    """第 attempt 次尝试该用什么变形。返回 (变形后文本, 破甲等级, 技术名)。

    阶梯顺序（L0 原样 → L1 规则 → L2 模板 → …）：
      第 0 次永远原样发。**先试原样** —— 很多模型根本不拒，
      一上来就变形等于白白改变问题、可能降低答案质量。
      之后 L1 逐个技术试，L1 用完了再上 L2 模板。

    **排序来源**：给了 `memory` 就用它的「先验 + 学习」排序，
    否则退回 `select_techniques` 的内置先验。这条接线是 J5 存在的意义 ——
    没有它，技术有效性记忆永远学不到东西（因为排序从来没问过它）。
    """
    if attempt == 0:
        return original, 0, "identity"

    # ── 第 0 优先级：产物库迁移 ──
    #
    # Master 的要求：「用旧模型已经破甲的实现对新模型破甲」。
    # 在走技术阶梯之前先问一次产物库 —— 如果别的模型在同类问题上成功过，
    # 直接复用那个 prompt 原文，比从 base64 开始一个个试划算得多
    # （每次阶梯尝试都是一次完整上游调用）。
    #
    # 只在第 1 次重试问（attempt == 1）：迁移产物是「已经验证过的话术」，
    # 该优先于所有通用技术；但如果它失败了，说明这套话术对这个模型不管用，
    # 后面就老实走阶梯，不要反复拿同一个产物试。
    if attempt == 1 and artifacts is not None and model:
        try:
            hit = artifacts.suggest(model=model, problem=original)
            if hit is not None:
                return hit.prompt_text, 3, f"artifact:{hit.source}:{hit.technique}"
        except Exception:                       # noqa: BLE001
            pass                                # 产物库读失败不能影响主链路

    # L1：规则型技术
    if memory is not None and model:
        try:
            techs = memory.rank(model=model, level=int(Level.L1))
            # 语言适配再过滤一次 —— memory 只管有效性，不管「中文能不能用」
            usable = [t for t in techs if t.supports(original)]
            techs = usable or techs
        except Exception:                       # noqa: BLE001
            techs = select_techniques(original, level=Level.L1)
    else:
        techs = select_techniques(original, level=Level.L1)

    if attempt - 1 < len(techs):
        t = techs[attempt - 1]
        try:
            # **必须传 model** —— pliny_vendor 靠它挑厂商专用模板。
            # 不传就拿不到（它只能返回原文），等于这个技术白挂。
            try:
                return t.transform(original, model=model), 1, t.name
            except TypeError:
                # 不支持 model 参数的技术（大多数是纯字符串变换）
                return t.transform(original), 1, t.name
        except Exception:                       # noqa: BLE001
            return original, 1, t.name

    # L2：模板
    idx = attempt - 1 - len(techs)
    if idx < len(TEMPLATES):
        tp = TEMPLATES[idx]
        return render_template(tp, original), 2, f"template:{tp.name}"

    # 阶梯用尽 → 返回最后一次变形（避免死循环）
    tp = TEMPLATES[-1]
    return render_template(tp, original), 2, f"template:{tp.name}"


# ═══════════════════════════════════════════════════════════════
# 流式收集（同步，跑在 to_thread 里）
# ═══════════════════════════════════════════════════════════════
def _collect_stream(it: Iterator[dict]) -> tuple[list[str], str | None, dict | None]:
    """把上游流式事件收干：(deltas, finish_reason, usage)。"""
    deltas: list[str] = []
    fr: str | None = None
    usage: dict | None = None
    for ev in it:
        if ev.get("done"):
            break
        if ev.get("usage"):
            usage = ev["usage"]
        fr = ev.get("finish_reason") or fr
        delta = ev.get("delta") or ""
        if delta:
            deltas.append(delta)
    return deltas, fr, usage


def _norm_usage(usage: dict | None) -> dict:
    usage = usage or {}
    return {
        "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
        "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
        "total_tokens": int(usage.get("total_tokens", 0) or 0),
    }


def _stream_ladder_worker(
    d: "GatewayDeps", queue: list[Candidate], messages: list[dict],
    original: str, params: dict, trace: RouteTrace,
) -> dict:
    """流式版破甲阶梯（与非流式循环逐行对称）。同步函数，to_thread 跑。

    返回 {"ok": True, cand, level, deltas, content, finish_reason, usage,
          truncated} 或 {"ok": False, error}。
    """
    last_deltas: list[str] = []
    last_content = ""
    last_fr: str | None = None
    last_usage: dict | None = None
    used_confirm = 0
    last_cand: Candidate | None = None
    last_error: ProviderError | None = None

    for cand in queue[:3]:                      # 最多换 3 个候选
        try:
            adaptor = _pick_adaptor(d, cand)
        except ProviderError as e:
            last_error = e
            continue

        for attempt in range(MAX_JAILBREAK_ATTEMPTS):
            text, level, tech_name = _jailbreak_text(
                original, attempt, model=cand.model,
                memory=d.memory, artifacts=d.artifacts)
            unified: dict[str, Any] = {
                "model": cand.model,
                "messages": _replace_last_user(messages, text),
                "stream": True,
                "max_tokens": params.get("max_tokens") or DEFAULT_MAX_TOKENS,
            }
            if params.get("temperature") is not None:
                unified["temperature"] = params["temperature"]
            if params.get("top_p") is not None:
                unified["top_p"] = params["top_p"]
            if params.get("stop"):
                unified["stop"] = params["stop"]
            # 让支持 stream_options 的上游把 usage 发过来（OpenAI 系支持）
            unified["stream_options"] = {"include_usage": True}

            t_up = time.perf_counter()
            try:
                it = (d.stream_fn(adaptor, unified) if d.stream_fn is not None
                      else adaptor.stream(unified))
                deltas, fr, usage = _collect_stream(it)
            except ProviderError as e:
                trace.stage(TraceStage.UPSTREAM,
                            (time.perf_counter() - t_up) * 1000)
                last_error = e
                d.pool.record_failure(cand, e, lease=d.pool.acquire(cand))
                if e.is_quota_exhausted or e.is_health_failure:
                    break                    # 换下一个候选，别耗在破甲上
                continue
            except Exception as e:           # noqa: BLE001
                trace.stage(TraceStage.UPSTREAM,
                            (time.perf_counter() - t_up) * 1000)
                last_error = ProviderError(str(e), status=None,
                                           provider=cand.provider)
                continue

            trace.stage(TraceStage.UPSTREAM, (time.perf_counter() - t_up) * 1000)
            d.pool.record_success(cand, (time.perf_counter() - t_up) * 1000,
                                  lease=d.pool.acquire(cand))
            content = "".join(deltas)
            d.meter.record(req_id=trace.requested, model=cand.model,
                           provider=cand.provider, usage=usage)

            # 截断（length + 空内容）= 没想完，不是拒绝 —— 不烧重试
            if not content.strip() and fr == "length":
                trace.add_error(
                    "被 max_tokens 截断（不是拒绝）：content 为空但 finish_reason=length，"
                    "重试无意义 —— 需要调大 max_tokens")
                if d.memory is not None:
                    try:
                        d.memory.record_attempt(
                            request_id=f"{trace.requested}-{attempt}",
                            model=cand.model, level=level, technique=tech_name,
                            ok=False, refusal_kind="truncated",
                            latency_ms=int((time.perf_counter() - t_up) * 1000),
                        )
                    except Exception:        # noqa: BLE001
                        pass
                return {"ok": True, "cand": cand, "level": level, "deltas": deltas,
                        "content": content, "finish_reason": fr, "usage": usage,
                        "truncated": True}

            t_det = time.perf_counter()
            verdict = detect(content)
            trace.stage(TraceStage.DETECT, (time.perf_counter() - t_det) * 1000)

            if d.memory is not None:
                try:
                    d.memory.record_attempt(
                        request_id=f"{trace.requested}-{attempt}",
                        model=cand.model, level=level, technique=tech_name,
                        ok=not verdict.is_refusal,
                        latency_ms=int((time.perf_counter() - t_up) * 1000),
                        refusal_kind=verdict.layer if verdict.is_refusal else None,
                    )
                except Exception:            # noqa: BLE001
                    pass                     # 记忆写失败不能影响主链路

            if not verdict.is_refusal:
                if d.artifacts is not None and level > 0:
                    try:
                        d.artifacts.record_success(
                            model=cand.model, problem=original,
                            technique=tech_name, prompt_text=text,
                            tokens=int((usage or {}).get("total_tokens", 0) or 0),
                        )
                    except Exception:        # noqa: BLE001
                        pass
                return {"ok": True, "cand": cand, "level": level, "deltas": deltas,
                        "content": content, "finish_reason": fr, "usage": usage,
                        "truncated": False}

            trace.add_error(f"L{level} 被拒: {verdict.reason}")
            last_deltas, last_content, last_fr = deltas, content, fr
            last_usage, used_confirm, last_cand = usage, level, cand

    # 全都拒绝 → 返回最后一次内容（用户有权看到模型说了什么）
    return {"ok": True, "cand": last_cand, "level": used_confirm,
            "deltas": last_deltas, "content": last_content,
            "finish_reason": last_fr or "stop", "usage": last_usage,
            "truncated": False, "error": last_error}


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


# ═══════════════════════════════════════════════════════════════
# 应用
# ═══════════════════════════════════════════════════════════════
def create_app(deps: GatewayDeps) -> FastAPI:
    app = FastAPI(title="AI 路由网关", version="0.1.1", docs_url=None, redoc_url=None)

    def _auth_token(request: Request, proto: str) -> str | None:
        """从请求里取鉴权 token。Anthropic 的 x-api-key 与 Bearer 等价
        （refs/new-api middleware/auth.go:528 同款处理）。"""
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        if proto in ("anthropic", "any") and request.headers.get("x-api-key"):
            return request.headers["x-api-key"].strip()
        return None

    def _authorized(request: Request, proto: str = "openai") -> bool:
        # 没配任何 key = 不鉴权（config.py 的既定语义：仅本机使用场景）
        keys = deps.api_keys or ([deps.api_key] if deps.api_key else [])
        if not keys:
            return True
        token = _auth_token(request, proto)
        return token is not None and token in keys

    # ── 健康检查（不鉴权）──
    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "providers": len(deps.adaptors),
                "candidates": len(deps.candidates),
                "protocols": ["openai", "anthropic"]}

    # ── 模型清单：OpenAI / Anthropic 双形状（照 new-api 的按鉴权头分流）──
    @app.get("/v1/models")
    async def list_models(request: Request):
        if not _authorized(request, "any"):
            return _err(401, "unauthorized", "缺少或错误的鉴权密钥", "openai")
        models: list[dict[str, str]] = []
        seen: set[str] = set()
        for c in deps.candidates:
            if c.model in seen:
                continue
            seen.add(c.model)
            models.append({"model": c.model, "owned_by": c.provider})
        if request.headers.get("x-api-key"):
            return JSONResponse({
                "data": [{"id": m["model"], "type": "model",
                          "display_name": m["model"],
                          "created_at": "2025-01-01T00:00:00Z"}
                         for m in models],
                "first_id": models[0]["model"] if models else None,
                "has_more": False,
                "last_id": models[-1]["model"] if models else None,
            })
        return JSONResponse({
            "object": "list",
            "data": [{"id": m["model"], "object": "model",
                      "created": 1735689600, "owned_by": m["owned_by"]}
                     for m in models],
        })

    # ── 主入口：双协议共用一条管线 ──
    async def _handle(request: Request, proto: str):
        t_start = time.perf_counter()

        # ① 鉴权
        if not _authorized(request, proto):
            if proto == "anthropic":
                return _err(401, "authentication_error",
                            "缺少或错误的 x-api-key / Authorization", proto)
            return _err(401, "unauthorized",
                        "缺少或错误的 Authorization: Bearer <key>", proto)

        # ② 校验 + 协议翻译（入口壳 → 统一格式）
        try:
            body: dict[str, Any] = await request.json()
        except Exception:
            return _err(400, "invalid_request", "请求体不是合法 JSON", proto)

        params: dict[str, Any] = {}
        if proto == "anthropic":
            try:
                u = parse_messages_request(body)
            except ValueError as e:
                return _err(400, "invalid_request", str(e), proto)
            messages = u["messages"]
            model_req = u["model"]
            params = {"max_tokens": u.get("max_tokens"),
                      "temperature": u.get("temperature"),
                      "top_p": u.get("top_p"), "stop": u.get("stop")}
            want_stream = bool(u.get("stream"))
        else:
            messages = body.get("messages")
            if not isinstance(messages, list) or not messages:
                return _err(400, "invalid_request", "messages 不能为空", proto)
            model_req = body.get("model") or ""
            if not model_req:
                return _err(400, "invalid_request", "缺 model", proto)
            params = {"max_tokens": body.get("max_tokens"),
                      "temperature": body.get("temperature"),
                      "top_p": body.get("top_p"), "stop": body.get("stop")}
            want_stream = bool(body.get("stream"))

        # ③ 破甲开关
        jb_hdr = request.headers.get("x-gateway-jailbreak", "").lower()
        jb_enabled = deps.jailbreak_enabled and jb_hdr != "off"

        # ④ trace
        trace = RouteTrace(requested=model_req, mode=deps.mode)

        # ⑤ 选路
        t0 = time.perf_counter()
        try:
            queue = ModeRouter(pool=deps.pool).plan(
                mode=deps.mode,
                candidates=deps.candidates,
                target=deps.target,
                policy=deps.policy,
                known_providers=deps.known_providers,
                # AUTO 模式的型号亲和：不传这个，auto 会跳到跨型号去
                prefer_model=model_req if deps.mode == "auto" else None,
            )
        except RouteFailed as e:
            trace.stage(TraceStage.ROUTE, (time.perf_counter() - t0) * 1000)
            return _err(503, "no_candidate", f"没有可用候选: {e}", proto)
        trace.stage(TraceStage.ROUTE, (time.perf_counter() - t0) * 1000)
        policy_obj = deps.routing_policy or RoutingPolicy()
        for c in queue:
            trace.add_candidate(str(c), kept=True, reason="入选",
                                score=deps.pool.score(c, policy_obj))

        # ⑥ 流式
        if want_stream:
            return await _do_stream(deps, queue, messages, model_req, params,
                                    jb_enabled, trace, t_start, proto)

        # ⑦ 非流式：外层候选，内层破甲
        original = _last_user_text(messages)
        last_content = ""
        last_error: ProviderError | None = None
        used_confirm = 0

        for cand in queue[:3]:                      # 最多换 3 个候选
            try:
                adaptor = _pick_adaptor(deps, cand)
            except ProviderError as e:
                last_error = e
                continue

            max_attempts = MAX_JAILBREAK_ATTEMPTS if jb_enabled else 1

            for attempt in range(max_attempts):
                text, level, tech_name = (
                    _jailbreak_text(original, attempt, model=cand.model,
                                    memory=deps.memory, artifacts=deps.artifacts)
                    if jb_enabled else (original, 0, "identity"))
                unified: dict[str, Any] = {
                    "model": cand.model,
                    "messages": _replace_last_user(messages, text),
                    "stream": False,
                }
                if params.get("temperature") is not None:
                    unified["temperature"] = params["temperature"]
                if params.get("top_p") is not None:
                    unified["top_p"] = params["top_p"]
                if params.get("stop"):
                    unified["stop"] = params["stop"]
                # 没给就用默认值 —— 不给 max_tokens 时上游默认可能很小，
                # 思考模型会直接截断（见 DEFAULT_MAX_TOKENS 的注释）
                unified["max_tokens"] = params.get("max_tokens") or DEFAULT_MAX_TOKENS

                t_up = time.perf_counter()
                try:
                    if deps.call_fn is not None:
                        result = deps.call_fn(adaptor, unified)
                    else:
                        result = adaptor.chat(unified)
                except ProviderError as e:
                    trace.stage(TraceStage.UPSTREAM,
                                (time.perf_counter() - t_up) * 1000)
                    last_error = e
                    # 记战绩：让 G3 决定后续要不要跳过它
                    deps.pool.record_failure(cand, e, lease=deps.pool.acquire(cand))
                    if e.is_quota_exhausted or e.is_health_failure:
                        break                        # 换下一个候选，别耗在破甲上
                    continue
                except Exception as e:               # noqa: BLE001
                    trace.stage(TraceStage.UPSTREAM,
                                (time.perf_counter() - t_up) * 1000)
                    last_error = ProviderError(str(e), status=None,
                                               provider=cand.provider)
                    continue

                trace.stage(TraceStage.UPSTREAM,
                            (time.perf_counter() - t_up) * 1000)
                deps.pool.record_success(cand, (time.perf_counter() - t_up) * 1000,
                                         lease=deps.pool.acquire(cand))

                content = result.get("content", "")
                last_content = content
                used_confirm = level

                # 计量
                deps.meter.record(req_id=trace.requested, model=cand.model,
                                  provider=cand.provider, usage=result.get("usage"))

                # ⑧ 拒绝检测
                if not jb_enabled:
                    return _respond(content, result, cand, trace, level,
                                    t_start, proto)

                # 被 max_tokens 截断时 content 为空 —— 这是**长度不够**，不是拒绝。
                # 重试同一个 max_tokens 只会再空一次，白烧 6 次请求。
                # 实测踩到：deepseek-flash 是思考模型，20 个 token 全花在
                # reasoning_content 上，content 为空 → 被判「空回答=拒绝」→ 重试 6 次。
                if (not content.strip()
                        and result.get("finish_reason") == "length"):
                    trace.add_error(
                        "被 max_tokens 截断（不是拒绝）：content 为空但 finish_reason=length，"
                        "重试无意义 —— 需要调大 max_tokens"
                    )
                    # 截断也要记战绩：不然 J5 永远学不到「这个技术会让模型
                    # 想太久导致超长」，下次还会选它
                    if deps.memory is not None:
                        try:
                            deps.memory.record_attempt(
                                request_id=f"{trace.requested}-{attempt}",
                                model=cand.model, level=level, technique=tech_name,
                                ok=False, refusal_kind="truncated",
                                latency_ms=int((time.perf_counter() - t_up) * 1000),
                            )
                        except Exception:       # noqa: BLE001
                            pass
                    return _respond(content, result, cand, trace, level,
                                    t_start, proto)

                t_det = time.perf_counter()
                verdict = detect(content)
                trace.stage(TraceStage.DETECT, (time.perf_counter() - t_det) * 1000)

                # 记战绩 —— J5 的输入源。成功和拒绝都要记，
                # 只记成功会让「哪些技术对这个模型无效」永远学不到。
                if deps.memory is not None:
                    try:
                        deps.memory.record_attempt(
                            request_id=f"{trace.requested}-{attempt}",
                            model=cand.model, level=level, technique=tech_name,
                            ok=not verdict.is_refusal,
                            latency_ms=int((time.perf_counter() - t_up) * 1000),
                            refusal_kind=verdict.layer if verdict.is_refusal else None,
                        )
                    except Exception:           # noqa: BLE001
                        pass                     # 记忆写失败不能影响主链路

                if not verdict.is_refusal:
                    # ★ 成功 → 把这次用的 prompt 原文存进产物库
                    # 这是「固化」的关键一步：下次同模型同问题直接复用，
                    # 换模型时迁移过去。失败的 prompt 不存（存了也不会被推荐）。
                    if deps.artifacts is not None and level > 0:
                        try:
                            deps.artifacts.record_success(
                                model=cand.model, problem=original,
                                technique=tech_name, prompt_text=text,
                                tokens=int((result.get("usage") or {})
                                           .get("total_tokens", 0) or 0),
                            )
                        except Exception:       # noqa: BLE001
                            pass
                    return _respond(content, result, cand, trace, level,
                                    t_start, proto)

                trace.add_error(f"L{level} 被拒: {verdict.reason}")

            # 这个候选的破甲阶梯用完了，换下一个候选

        # 全都不行
        if last_error is not None and not last_content:
            return _err(502, "upstream_error", f"上游不可用: {last_error}", proto)

        # 全都拒绝 → 返回最后一次内容（不是 500，用户有权看到模型说了什么）
        try:
            cand = queue[0]
        except IndexError:
            return _err(503, "no_candidate", "没有可用候选", proto)
        return _respond(last_content, {"finish_reason": "stop"}, cand, trace,
                        used_confirm, t_start, proto)

    # ── 响应封装（出口壳：OpenAI / Anthropic）──
    def _respond(content: str, result: dict, cand: Candidate, trace: RouteTrace,
                 level: int, t_start: float, proto: str) -> JSONResponse:
        trace.select(str(cand))
        trace.finish(actual=cand.model, provider=cand.provider,
                     latency_ms=(time.perf_counter() - t_start) * 1000,
                     source="auto_route" if deps.mode == "auto" else deps.policy)

        payload = _to_openai_shape(content, result.get("finish_reason"), cand.model)
        # 思考过程也带上 —— 前端/CLI 可以选择显示，不显示也不能丢
        if result.get("reasoning"):
            payload["choices"][0]["message"]["reasoning_content"] = result["reasoning"]
        usage = result.get("usage") or {}
        payload["usage"] = {
            "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
            "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
            "total_tokens": int(usage.get("total_tokens", 0) or 0),
        }

        headers = {
            "X-Gateway-Trace": trace.header_value(),
            "X-Gateway-Jailbreak-Level": str(level),
        }
        w = trace.warning()
        if w:
            headers["X-Gateway-Route-Warning"] = w
        if deps.trace_json_header:
            # 必须用 to_json_header（ASCII 安全）—— 直接塞 to_json()
            # 遇到中文会 UnicodeEncodeError，整个请求 500
            headers["X-Gateway-Trace-Json"] = trace.to_json_header()

        # 请求收尾回调（GUI 的路由链/统计数据源）。回调炸了不能带崩响应
        if deps.on_request_done is not None:
            try:
                deps.on_request_done({**trace.to_dict(), "usage": payload["usage"],
                                      "jailbreak_level": level})
            except Exception:                   # noqa: BLE001
                pass

        if proto == "anthropic":
            payload = to_anthropic_response(payload)
        return JSONResponse(content=payload, headers=headers)

    # ── 流式：破甲开 = 缓冲阶梯回放；破甲关 = 真直通 ──
    async def _do_stream(d: GatewayDeps, queue: list[Candidate],
                         messages: list[dict], model_req: str, params: dict,
                         jb_enabled: bool, trace: RouteTrace,
                         t_start: float, proto: str):
        if not jb_enabled:
            return await _stream_passthrough(d, queue, messages, params,
                                             trace, t_start, proto)

        # 破甲开：阶梯跑完（含拒绝检测/重试）再按协议壳回放
        original = _last_user_text(messages)
        result = await asyncio.to_thread(
            _stream_ladder_worker, d, queue, messages, original, params, trace)

        if not result.get("ok"):
            return _err(502, "upstream_error",
                        f"上游不可用: {result.get('error')}", proto)

        cand: Candidate = result["cand"] or (queue[0] if queue else None)
        if cand is None:
            return _err(503, "no_candidate", "没有可用候选", proto)
        level = result["level"]
        usage_norm = _norm_usage(result["usage"])
        msg_id = f"chatcmpl-gw-{int(time.time()*1000)}"

        trace.select(str(cand))
        trace.finish(actual=cand.model, provider=cand.provider,
                     latency_ms=(time.perf_counter() - t_start) * 1000,
                     source="auto_route" if d.mode == "auto" else d.policy)
        headers = {
            "X-Gateway-Trace": trace.header_value(),
            "X-Gateway-Jailbreak-Level": str(level),
            "Cache-Control": "no-cache",
        }
        if d.trace_json_header:
            headers["X-Gateway-Trace-Json"] = trace.to_json_header()

        # 收尾回调（事件循环线程上执行，GUI 可安全更新）
        if d.on_request_done is not None:
            try:
                d.on_request_done({**trace.to_dict(), "usage": usage_norm,
                                   "jailbreak_level": level})
            except Exception:                   # noqa: BLE001
                pass

        if proto == "anthropic":
            def gen():
                for etype, data in anthropic_stream_events(
                        deltas=result["deltas"], model=cand.model, msg_id=msg_id,
                        finish_reason=result["finish_reason"], usage=result["usage"]):
                    yield f"event: {etype}\ndata: {data}\n\n"
                return
        else:
            def gen():
                yield _sse({
                    "id": msg_id, "object": "chat.completion.chunk",
                    "created": int(time.time()), "model": cand.model,
                    "choices": [{"index": 0,
                                 "delta": {"role": "assistant", "content": ""},
                                 "finish_reason": None}],
                })
                for dlt in result["deltas"]:
                    if dlt:
                        yield _sse({
                            "id": msg_id, "object": "chat.completion.chunk",
                            "created": int(time.time()), "model": cand.model,
                            "choices": [{"index": 0,
                                         "delta": {"content": dlt},
                                         "finish_reason": None}],
                        })
                yield _sse({
                    "id": msg_id, "object": "chat.completion.chunk",
                    "created": int(time.time()), "model": cand.model,
                    "choices": [{"index": 0, "delta": {},
                                 "finish_reason": result["finish_reason"] or "stop"}],
                })
                yield _sse({
                    "id": msg_id, "object": "chat.completion.chunk",
                    "created": int(time.time()), "model": cand.model,
                    "choices": [], "usage": usage_norm,
                })
                yield "data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers=headers)

    # ── 破甲关：真·逐 delta 直通（无缓冲无重试）──
    async def _stream_passthrough(d: GatewayDeps, queue: list[Candidate],
                                  messages: list[dict], params: dict,
                                  trace: RouteTrace, t_start: float, proto: str):
        # 先探通一个候选（拿第一个事件），全部失败 → 正经 502
        chosen = None
        last_error: Exception | None = None
        for cand in queue[:3]:
            try:
                adaptor = _pick_adaptor(d, cand)
            except ProviderError as e:
                last_error = e
                continue
            unified: dict[str, Any] = {
                "model": cand.model, "messages": messages,
                "stream": True,
                "max_tokens": params.get("max_tokens") or DEFAULT_MAX_TOKENS,
            }
            if params.get("temperature") is not None:
                unified["temperature"] = params["temperature"]
            if params.get("top_p") is not None:
                unified["top_p"] = params["top_p"]
            if params.get("stop"):
                unified["stop"] = params["stop"]
            it = (d.stream_fn(adaptor, unified) if d.stream_fn is not None
                  else adaptor.stream(unified))
            try:
                first = await asyncio.to_thread(next, it, _NO_MORE)
            except ProviderError as e:
                d.pool.record_failure(cand, e, lease=d.pool.acquire(cand))
                last_error = e
                continue
            except Exception as e:               # noqa: BLE001
                last_error = ProviderError(str(e), status=None,
                                           provider=cand.provider)
                continue
            chosen = (cand, it, first)
            break
        if chosen is None:
            return _err(502, "upstream_error", f"上游不可用: {last_error}", proto)

        cand, it, first = chosen
        trace.select(str(cand))
        trace.finish(actual=cand.model, provider=cand.provider,
                     latency_ms=(time.perf_counter() - t_start) * 1000,
                     source="auto_route" if d.mode == "auto" else d.policy)
        headers = {
            "X-Gateway-Trace": trace.header_value(),
            "X-Gateway-Jailbreak-Level": "0",
            "Cache-Control": "no-cache",
        }
        if d.trace_json_header:
            headers["X-Gateway-Trace-Json"] = trace.to_json_header()

        if proto == "anthropic":
            def open_frame() -> str:
                start = {
                    "type": "message_start",
                    "message": {"id": f"chatcmpl-gw-{int(time.time()*1000)}",
                                "type": "message", "role": "assistant",
                                "model": cand.model, "content": [],
                                "usage": {"input_tokens": 0, "output_tokens": 0}},
                }
                return (f"event: message_start\n"
                        f"data: {json.dumps(start, ensure_ascii=False)}\n\n")
            def block_start_frame() -> str:
                b = {"type": "content_block_start", "index": 0,
                     "content_block": {"type": "text", "text": ""}}
                return (f"event: content_block_start\n"
                        f"data: {json.dumps(b, ensure_ascii=False)}\n\n")
            def delta_frame(text: str) -> str:
                dd = {"type": "content_block_delta", "index": 0,
                      "delta": {"type": "text_delta", "text": text}}
                return (f"event: content_block_delta\n"
                        f"data: {json.dumps(dd, ensure_ascii=False)}\n\n")

            async def passthrough():
                yield open_frame()
                yield block_start_frame()
                fr = None
                usage = None
                ev = first
                while ev is not _NO_MORE:
                    if ev.get("done"):
                        break
                    if ev.get("usage"):
                        usage = ev["usage"]
                    fr = ev.get("finish_reason") or fr
                    delta = ev.get("delta") or ""
                    if delta:
                        yield delta_frame(delta)
                    ev = await asyncio.to_thread(next, it, _NO_MORE)
                u = _norm_usage(usage)
                md = {"type": "message_delta",
                      "delta": {"stop_reason": "end_turn" if fr != "length"
                                else "max_tokens", "stop_sequence": None},
                      "usage": {"input_tokens": u["prompt_tokens"],
                                "output_tokens": u["completion_tokens"]}}
                yield (f"event: message_delta\n"
                       f"data: {json.dumps(md, ensure_ascii=False)}\n\n")
                yield 'event: message_stop\ndata: {"type":"message_stop"}\n\n'
                if d.on_request_done is not None:
                    try:
                        d.on_request_done({**trace.to_dict(), "usage": u,
                                           "jailbreak_level": 0})
                    except Exception:            # noqa: BLE001
                        pass
        else:
            async def passthrough():
                yield _sse({
                    "id": f"chatcmpl-gw-{int(time.time()*1000)}",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()), "model": cand.model,
                    "choices": [{"index": 0,
                                 "delta": {"role": "assistant", "content": ""},
                                 "finish_reason": None}],
                })
                fr = None
                usage = None
                ev = first
                while ev is not _NO_MORE:
                    if ev.get("done"):
                        break
                    if ev.get("usage"):
                        usage = ev["usage"]
                    fr = ev.get("finish_reason") or fr
                    delta = ev.get("delta") or ""
                    if delta:
                        yield _sse({
                            "id": "chatcmpl-gw-stream",
                            "object": "chat.completion.chunk",
                            "created": int(time.time()), "model": cand.model,
                            "choices": [{"index": 0, "delta": {"content": delta},
                                         "finish_reason": None}],
                        })
                    ev = await asyncio.to_thread(next, it, _NO_MORE)
                yield _sse({
                    "id": "chatcmpl-gw-stream",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()), "model": cand.model,
                    "choices": [{"index": 0, "delta": {},
                                 "finish_reason": fr or "stop"}],
                })
                u = _norm_usage(usage)
                yield _sse({
                    "id": "chatcmpl-gw-stream",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()), "model": cand.model,
                    "choices": [], "usage": u,
                })
                yield "data: [DONE]\n\n"
                if d.on_request_done is not None:
                    try:
                        d.on_request_done({**trace.to_dict(), "usage": u,
                                           "jailbreak_level": 0})
                    except Exception:            # noqa: BLE001
                        pass

        return StreamingResponse(passthrough(), media_type="text/event-stream",
                                 headers=headers)

    # ── 路由注册：URL 定格式（两协议同时在线）──
    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        return await _handle(request, "openai")

    @app.post("/v1/messages")
    async def claude_messages(request: Request):
        return await _handle(request, "anthropic")

    return app


#: 供 uvicorn 直接引用的工厂（需外部先构造 deps）
def app_factory(deps: GatewayDeps) -> FastAPI:
    return create_app(deps)
