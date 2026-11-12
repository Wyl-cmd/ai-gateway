"""G8 接入层 —— FastAPI 应用，把前面所有零件装成机器。

这是第一个「能真跑」的批次。链路：

    鉴权 → 校验 → 路由选路 → 破甲变形 → 调上游 → 拒绝检测 → 升级重试 → 计量

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
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

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
DEFAULT_MAX_TOKENS = 4000


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
    trace_json_header: bool = False


# ═══════════════════════════════════════════════════════════════
# 工具
# ═══════════════════════════════════════════════════════════════
def _err(status: int, etype: str, msg: str) -> JSONResponse:
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
    """按 (provider, model) 找适配器；找不到就按 provider 兜底。"""
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
# 应用
# ═══════════════════════════════════════════════════════════════
def create_app(deps: GatewayDeps) -> FastAPI:
    app = FastAPI(title="AI 路由网关", version="0.1.0", docs_url=None, redoc_url=None)

    def _authorized(request: Request) -> bool:
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return False
        return auth[7:].strip() == deps.api_key

    # ── 健康检查（不鉴权）──
    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "providers": len(deps.adaptors),
                "candidates": len(deps.candidates)}

    # ── 主入口 ──
    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        t_start = time.perf_counter()

        # ① 鉴权
        if not _authorized(request):
            return _err(401, "unauthorized", "缺少或错误的 Authorization: Bearer <key>")

        # ② 校验
        try:
            body: dict[str, Any] = await request.json()
        except Exception:
            return _err(400, "invalid_request", "请求体不是合法 JSON")
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            return _err(400, "invalid_request", "messages 不能为空")
        model_req = body.get("model") or ""
        if not model_req:
            return _err(400, "invalid_request", "缺 model")
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
            return _err(503, "no_candidate", f"没有可用候选: {e}")
        trace.stage(TraceStage.ROUTE, (time.perf_counter() - t0) * 1000)
        policy_obj = RoutingPolicy()
        for c in queue:
            trace.add_candidate(str(c), kept=True, reason="入选",
                                score=deps.pool.score(c, policy_obj))

        # ⑥ 流式
        if want_stream:
            return await _do_stream(deps, request, queue, messages, model_req,
                                    jb_enabled, trace, t_start)

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
                unified = {
                    "model": cand.model,
                    "messages": _replace_last_user(messages, text),
                    "stream": False,
                }
                if body.get("temperature") is not None:
                    unified["temperature"] = body["temperature"]
                # 没给就用默认值 —— 不给 max_tokens 时上游默认可能很小，
                # 思考模型会直接截断（见 DEFAULT_MAX_TOKENS 的注释）
                unified["max_tokens"] = body.get("max_tokens") or DEFAULT_MAX_TOKENS

                t_up = time.perf_counter()
                try:
                    if deps.call_fn is not None:
                        result = deps.call_fn(adaptor, unified)
                    else:
                        result = adaptor.chat(unified)
                except ProviderError as e:
                    trace.stage(TraceStage.UPSTREAM, (time.perf_counter() - t_up) * 1000)
                    last_error = e
                    # 记战绩：让 G3 决定后续要不要跳过它
                    deps.pool.record_failure(cand, e, lease=deps.pool.acquire(cand))
                    if e.is_quota_exhausted or e.is_health_failure:
                        break                        # 换下一个候选，别耗在破甲上
                    continue
                except Exception as e:               # noqa: BLE001
                    trace.stage(TraceStage.UPSTREAM, (time.perf_counter() - t_up) * 1000)
                    last_error = ProviderError(str(e), status=None, provider=cand.provider)
                    continue

                trace.stage(TraceStage.UPSTREAM, (time.perf_counter() - t_up) * 1000)
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
                    return _respond(content, result, cand, trace, level, t_start, deps)

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
                                    t_start, deps)

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
                    return _respond(content, result, cand, trace, level, t_start, deps)

                trace.add_error(f"L{level} 被拒: {verdict.reason}")

            # 这个候选的破甲阶梯用完了，换下一个候选

        # 全都不行
        if last_error is not None and not last_content:
            return _err(502, "upstream_error", f"上游不可用: {last_error}")

        # 全都拒绝 → 返回最后一次内容（不是 500，用户有权看到模型说了什么）
        try:
            cand = queue[0]
        except IndexError:
            return _err(503, "no_candidate", "没有可用候选")
        return _respond(last_content, {"finish_reason": "stop"}, cand, trace,
                        used_confirm, t_start, deps)

    # ── 响应封装 ──
    def _respond(content: str, result: dict, cand: Candidate, trace: RouteTrace,
                 level: int, t_start: float, d: GatewayDeps) -> JSONResponse:
        trace.select(str(cand))
        trace.finish(actual=cand.model, provider=cand.provider,
                     latency_ms=(time.perf_counter() - t_start) * 1000,
                     source="auto_route" if d.mode == "auto" else d.policy)

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
        if d.trace_json_header:
            # 必须用 to_json_header（ASCII 安全）—— 直接塞 to_json()
            # 遇到中文会 UnicodeEncodeError，整个请求 500
            headers["X-Gateway-Trace-Json"] = trace.to_json_header()

        return JSONResponse(content=payload, headers=headers)

    # ── 流式 ──
    async def _do_stream(d: GatewayDeps, request: Request, queue: list[Candidate],
                         messages: list[dict], model_req: str, jb_enabled: bool,
                         trace: RouteTrace, t_start: float):
        cand = queue[0]
        try:
            adaptor = _pick_adaptor(d, cand)
        except ProviderError as e:
            return _err(502, "upstream_error", str(e))

        unified = {"model": cand.model, "messages": messages, "stream": True}

        def gen() -> Iterator[str]:
            trace.select(str(cand))
            trace.finish(actual=cand.model, provider=cand.provider,
                         latency_ms=(time.perf_counter() - t_start) * 1000,
                         source="auto_route" if d.mode == "auto" else d.policy)
            try:
                it = (d.stream_fn(adaptor, unified) if d.stream_fn is not None
                      else adaptor.stream(unified))
                for ev in it:
                    if ev.get("done"):
                        break
                    delta = ev.get("delta", "")
                    if delta:
                        chunk = {
                            "id": "chatcmpl-gw-stream",
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": cand.model,
                            "choices": [{"index": 0, "delta": {"content": delta},
                                         "finish_reason": None}],
                        }
                        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
            except Exception as e:                   # noqa: BLE001
                err = {"error": {"type": "stream_error", "message": str(e)}}
                yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"

        headers = {
            "X-Gateway-Trace": trace.header_value(),
            "X-Gateway-Jailbreak-Level": "0",
            "Cache-Control": "no-cache",
        }
        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers=headers)

    return app


#: 供 uvicorn 直接引用的工厂（需外部先构造 deps）
def app_factory(deps: GatewayDeps) -> FastAPI:
    return create_app(deps)
