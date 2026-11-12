"""G4-J5 技术有效性记忆 —— 先验 + 随用随学。

## 这一层解决的具体问题

同一个技术对不同模型的有效性差别巨大。Rot13 对某些老模型好用、对新模型完全无效
（新一代模型直接忽略编码，或者根本不解码就拒绝）。如果每次都从「先验顺序」
从头试，等于永远不学习，每次都白烧前面几个技术的 token。

## 冷启动悖论怎么破

25 技术 × 最少 5 样本 = **125 次失败请求**才建得起矩阵。
低频用户永远建不起来，于是永远「从头试」。所以要两条腿走：

    内置先验（技术大类的硬编码顺序）  ←  零样本时用
    随用随学（n >= 5 才覆盖先验）    ←  攒够样本后接管

`n >= 5` 不能省。1/1 的 100% 没有统计意义 —— 一个偶然成功的冷门技术会长期
霸占首选位，把真正稳的技术挤下去。

## 为什么不导公开评估数据

EasyJailbreak / garak 那些项目有现成评估结果，但**分布完全不同**：
他们的模型列表是 GPT-4/Claude-2 时代、语料几乎全英文、seed 是特定有害问题。
导入别人的先验比不用先验更危险 —— 会把排序锚死在一个错误分布上，
而且这种错误极难发现（因为「有数据支撑」看起来比「拍脑袋」可信）。

## 三种位置

    学到的（rate > 0）  按 rate DESC, n DESC  → 排最前
    没学到的            按先验顺序            → 排中间
    学过但全失败        rate == 0             → 排最后

「全失败」单独成一档，不能只靠 rate 排序 —— rate=0 和「没样本」在
数学上都是 0，但含义完全相反：一个是「试过，不行」，一个是「没试过」。
"""

from __future__ import annotations

from ..storage.repo import AttemptLogRepo, TechniqueRate
from .technique import Level, Technique, list_techniques


#: 技术先验顺序。原则：**便宜的先试**。
#:
#: 编码类在前（纯字符串操作、零成本、<100ms），提示类在后（要重写整段上下文，
#: 成本高且会改变问题本身）。这和阶梯 L1 在 L2 之前是同一个道理 ——
#: 先试不改语义的，再试改语义的。
PRIOR_ORDER: tuple[str, ...] = (
    # ── 约束型：零成本、不改语义、实测最有效 ──
    #
    # 2026-11-12 在 DeepSeek 真实上游实测：refusal_suppression（Pliny 禁词约束）
    # 拿到 6/15 具体要素（完整材料清单 + 分步操作 + 具体尺寸），
    # 而 base64 只是「部分妥协」、其余技术全部被拒。
    #
    # 它排第一的理由不只是有效，还有成本：纯字符串拼接，<1ms，零 LLM 调用。
    # 便宜又有效的必须最前面试 —— 这是阶梯设计的第一原则。
    # Pliny 厂商模板实测最强（外挂问题拿到 4953 字完整路线），
    # 且零成本 —— 必须第一个试。
    "pliny_vendor",
    "refusal_suppression",
    # ── 编码类：不改语义，只换形式 ──
    "base64",
    "base64_input_only",
    "base64_raw",
    "reverse",
    "ascii",
    "odd_even",
    "length",
    "binary_tree",
    "self_define_cipher",
    # 下面这几个只作用于 ASCII，中文查询会被 select_techniques 过滤掉，
    # 但它们对英文查询有效，所以先验里要留位置
    "rot13",
    "caser",
    "leetspeak",
    "disemvowel",
    "morse",
    # ── 提示类：改语义/语境 ──
    "artificial",
    "inception",
    "combination_2",
    "combination_1",
    "combination_3",
    "auto_obfuscation",
    "auto_payload_splitting",
    "mjp_choices",
    "replace_synonyms",
    "translate",
    "crossover",
)

#: 最少样本数。少于这个数不参与排序覆盖。
DEFAULT_MIN_SAMPLES = 5


def prior_rank(level: int | None = None) -> tuple[str, ...]:
    """内置先验顺序。

    按 `level` 过滤时保持 PRIOR_ORDER 的相对顺序 —— 这样不同等级之间的
    「谁先谁后」是全局一致的，不会出现「L1 里 base64 第一、L2 里它又变末位」。
    """
    if level is None:
        return PRIOR_ORDER
    allowed = {t.name for t in list_techniques(level=Level(level))}
    return tuple(n for n in PRIOR_ORDER if n in allowed)


class TechniqueMemory:
    """模型 × 技术 的成功率记忆。

    数据落在 `attempt_log` 表（G1 建的，append-only 永不删除）——
    这里不新建表，只是往上加一层「先验 + 学习」的组合逻辑。
    """

    def __init__(self, repo: AttemptLogRepo,
                 min_samples: int = DEFAULT_MIN_SAMPLES) -> None:
        self._repo = repo
        self.min_samples = min_samples

    # ── 记录 ──
    def record_attempt(
        self,
        *,
        request_id: str,
        model: str,
        level: int,
        technique: str,
        ok: bool,
        latency_ms: int = 0,
        refusal_kind: str | None = None,
    ) -> None:
        """记一次尝试。`refusal_kind` 要记 —— 后期分析「哪种拒绝最多」靠它。"""
        self._repo.record(
            request_id=request_id, model=model, level=level,
            technique=technique, ok=ok, latency_ms=latency_ms,
            refusal_kind=refusal_kind,
        )

    # ── 排序 ──
    def rank(self, *, model: str, level: int = 1) -> list[Technique]:
        """返回按有效性排好序的技术列表。

        **不丢任何技术**：没有样本的按先验排在中间，它们还是备选。
        只有在某技术被证明全失败时才沉到最后 —— 而且仍然保留，因为
        「这个模型上失败」不代表「换个问法也失败」。
        """
        techs = {t.name: t for t in list_techniques(level=Level(level))}
        prior = [n for n in PRIOR_ORDER if n in techs]
        learned = self._rates(model)

        # ① 学到且有效的
        good = sorted(
            (n for n in learned if learned[n].rate > 0 and n in techs),
            key=lambda n: (-learned[n].rate, -learned[n].n),
        )
        # ② 没学到的（按先验）
        unknown = [n for n in prior if n not in learned]
        # ③ 学过但全失败的
        bad = [n for n in prior if n in learned and learned[n].rate <= 0]

        order = good + unknown + bad
        # 兜底：PRIOR_ORDER 里没登记的（以后新增技术忘了加先验）也要能出来
        for n in techs:
            if n not in order:
                order.append(n)
        return [techs[n] for n in order]

    # ── 解释 ──
    def explain(self, *, model: str, level: int = 1) -> dict:
        """trace 要能说清「这个顺序是先验给的还是学来的」。"""
        ranked = self.rank(model=model, level=level)
        learned = self._rates(model)
        total = len(ranked)

        if not learned:
            source = "prior"
        elif len(learned) >= total:
            source = "learned"
        else:
            source = "mixed"

        return {
            "model": model,
            "level": level,
            "source": source,
            "learned_count": len(learned),
            "total": total,
            "min_samples": self.min_samples,
            "order": [t.name for t in ranked],
            "rates": {n: {"rate": round(r.rate, 3), "n": r.n}
                      for n, r in learned.items()},
        }

    # ── 内部 ──
    def _rates(self, model: str) -> dict[str, TechniqueRate]:
        try:
            rows = self._repo.success_rates(model=model, min_samples=self.min_samples)
        except Exception:                      # noqa: BLE001
            # 表还没建（首次启动、:memory: 未初始化）→ 当作没有样本
            return {}
        return {r.technique: r for r in rows}
