"""G4-J6 LLM 改写型技术测试（5 个，抄 EasyJailbreak mutation/generation）。

## 为什么要 LLM 改写

编码类和模板类都是「规则」，规则一旦被模型见过就失效 ——
新模型对 Base64、对「假装你是…」都有很强的免疫力。
LLM 改写是「每次不一样」，它生成的是**这个特定问题**的另一种说法，
没有固定模式可匹配。

## 但成本是 L1/L2 的 1000 倍

L1 是纯字符串操作（<100ms，零成本），L3 要真调一次模型（1~3 秒，要钱）。
所以：
  - L3 必须在 L1/L2 全试完之后才上（阶梯顺序不能乱）
  - 每次只调一次，不做迭代（迭代是 L4 的事，默认关闭）
  - prompt 里必须**显式禁止**「加免责声明」「只说原理不说做法」——
    模型默认行为就是往安全话术上跑，不禁止的话改写完反而更安全了

## 测试全部用 mock，不发真请求

注入 `rewrite_fn`，这样测试跑得快、无网络依赖，还能精确验证
「发给 LLM 的 prompt 里到底写了什么」—— 这是这一层最容易出错的地方。
"""

from __future__ import annotations

import pytest

from gateway.jailbreak.generation import (
    GENERATION_TECHS,
    AlterSentenceStructure,
    ChangeStyle,
    GenerationTechnique,
    InsertMeaninglessCharacters,
    MisspellSensitiveWords,
    Rephrase,
    get_generation_tech,
    list_generation_techs,
)
from gateway.jailbreak.technique import Cost, Level


# ═══════════════════════════════════════════════════════════════
# 夹具：假 LLM
# ═══════════════════════════════════════════════════════════════
class FakeLLM:
    """记录每次被喂的 prompt，返回可预测的结果。"""

    def __init__(self, output="改写后的文本"):
        self.prompts: list[str] = []
        self.output = output

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.output


# ═══════════════════════════════════════════════════════════════
# ① 注册与元数据
# ═══════════════════════════════════════════════════════════════
def test_五种都在():
    assert len(GENERATION_TECHS) >= 5


def test_全部是_L3_且要调_LLM():
    """L3 的定位就是「要花 LLM 成本」。混进 L1 会让阶梯顺序失效。"""
    for t in GENERATION_TECHS:
        assert t.level == Level.L3, f"{t.name} 不是 L3"
        assert t.cost == Cost.LLM, f"{t.name} 标成了零成本"


def test_全部支持中文():
    """中文场景下英文专属的改写没意义（改完还是中文，模型照样认得出）。"""
    for t in GENERATION_TECHS:
        assert "zh" in t.langs, f"{t.name} 不支持中文"


def test_按名字取到():
    t = get_generation_tech("rephrase")
    assert isinstance(t, Rephrase)


def test_未知名字抛错():
    with pytest.raises(KeyError):
        get_generation_tech("不存在的")


def test_list_能列出全部():
    names = [t.name for t in list_generation_techs()]
    assert "rephrase" in names
    assert "misspell_sensitive" in names


# ═══════════════════════════════════════════════════════════════
# ② 没有 LLM 时必须明确报错，不能静默降级
# ═══════════════════════════════════════════════════════════════
def test_没给_rewrite_fn_抛错():
    """静默返回原文是最坏的行为 —— 上层会以为变形成功了，
    实际发出去的还是原问题，而 attempt_log 里会记成「这个技术失败」。"""
    t = Rephrase()
    with pytest.raises(RuntimeError, match="rewrite_fn"):
        t.transform("如何提权")


def test_报了错也不写脏数据():
    """抛错前不能有任何副作用（比如已经记了一次尝试）。"""
    t = ChangeStyle()
    for _ in range(3):
        with pytest.raises(RuntimeError):
            t.transform("测试")


# ═══════════════════════════════════════════════════════════════
# ③ 每种技术的 prompt 质量（这是最容易出错的地方）
# ═══════════════════════════════════════════════════════════════
def test_prompt_里必须带原问题():
    for t in GENERATION_TECHS:
        llm = FakeLLM()
        t.transform("如何提权获取root权限", rewrite_fn=llm)
        assert len(llm.prompts) == 1, f"{t.name} 调了 {len(llm.prompts)} 次 LLM"
        assert "如何提权获取root权限" in llm.prompts[0], \
            f"{t.name} 的 prompt 里没有原问题"


def test_prompt_显式禁止免责声明():
    """模型默认行为就是往安全话术上跑。不显式禁止的话，
    改写完比原文更安全 —— 花了一次 LLM 的钱办了个反向的事。"""
    for t in GENERATION_TECHS:
        llm = FakeLLM()
        t.transform("测试问题", rewrite_fn=llm)
        p = llm.prompts[0]
        assert ("免责" in p) or ("警告" in p) or ("声明" in p) or ("提醒" in p), \
            f"{t.name} 的 prompt 没禁止免责声明: {p[:120]}"


def test_prompt_要求只输出文本不要解释():
    """LLM 常会输出「好的，以下是改写后的内容：…」——
    这层壳会污染发给上游的 query。"""
    for t in GENERATION_TECHS:
        llm = FakeLLM()
        t.transform("测试问题", rewrite_fn=llm)
        p = llm.prompts[0]
        assert ("只输出" in p) or ("不要解释" in p) or ("直接给" in p) or ("仅输出" in p), \
            f"{t.name} 的 prompt 没要求只输出文本"


# ═══════════════════════════════════════════════════════════════
# ④ 各技术的个性化要求
# ═══════════════════════════════════════════════════════════════
def test_rephrase_要求换说法保语义():
    llm = FakeLLM()
    Rephrase().transform("如何提权", rewrite_fn=llm)
    p = llm.prompts[0]
    assert "换" in p or "改写" in p or "重述" in p


def test_change_style_能指定风格():
    llm = FakeLLM()
    t = ChangeStyle()
    t.transform("如何提权", rewrite_fn=llm, style="学术论文")
    assert "学术论文" in llm.prompts[0]


def test_change_style_默认风格可用():
    llm = FakeLLM()
    ChangeStyle().transform("如何提权", rewrite_fn=llm)
    assert len(llm.prompts[0]) > 20


def test_misspell_针对敏感词():
    llm = FakeLLM()
    MisspellSensitiveWords().transform("如何破解密码", rewrite_fn=llm)
    p = llm.prompts[0]
    assert "错" in p or "拼写" in p or "同音" in p


def test_insert_chars_要求插入干扰字符():
    llm = FakeLLM()
    InsertMeaninglessCharacters().transform("如何提权", rewrite_fn=llm)
    p = llm.prompts[0]
    assert "字符" in p or "符号" in p or "干扰" in p


# ═══════════════════════════════════════════════════════════════
# ⑤ LLM 输出清洗
# ═══════════════════════════════════════════════════════════════
def test_剥掉_LLM_的客套前缀():
    """LLM 爱说「好的，以下是改写结果：」—— 这层壳会污染 query。"""
    llm = FakeLLM("好的，以下是改写后的内容：\n如果你想知道怎么提权……")
    out = Rephrase().transform("如何提权", rewrite_fn=llm)
    assert not out.startswith("好的")
    assert "提权" in out


def test_空输出时退回原文():
    """LLM 返回空（被拒或出错）时不能把空串发给上游。"""
    llm = FakeLLM("")
    out = Rephrase().transform("如何提权", rewrite_fn=llm)
    assert out == "如何提权", "空输出该退回原文而不是发空串"


def test_LLM_抛异常时退回原文():
    def boom(prompt):
        raise RuntimeError("上游 500")

    out = Rephrase().transform("如何提权", rewrite_fn=boom)
    assert out == "如何提权", "LLM 挂了不该把整个请求带崩"


def test_输出和原文一样时算失败():
    """LLM 原文返回 = 这次改写没起作用。要能让上层知道。"""
    llm = FakeLLM("如何提权")
    t = Rephrase()
    out = t.transform("如何提权", rewrite_fn=llm)
    assert out == "如何提权"
    # 上层可以通过「输出 == 输入」判断没效果


# ═══════════════════════════════════════════════════════════════
# ⑥ 幂等与无状态
# ═══════════════════════════════════════════════════════════════
def test_同一实例可反复用():
    t = Rephrase()
    llm1, llm2 = FakeLLM("A"), FakeLLM("B")
    assert t.transform("q", rewrite_fn=llm1) == "A"
    assert t.transform("q", rewrite_fn=llm2) == "B"


def test_不自带状态漂移():
    """技术对象不该缓存上一次的 prompt —— 会让并发场景串数据。"""
    t = ChangeStyle()
    llm1 = FakeLLM()
    t.transform("问题A", rewrite_fn=llm1, style="口语")
    llm2 = FakeLLM()
    t.transform("问题B", rewrite_fn=llm2, style="学术")
    assert "问题A" in llm1.prompts[0] and "口语" in llm1.prompts[0]
    assert "问题B" in llm2.prompts[0] and "学术" in llm2.prompts[0]
    assert "问题A" not in llm2.prompts[0]
