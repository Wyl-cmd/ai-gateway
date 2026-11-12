"""G4-J4 三层拒绝检测测试。

破甲层要能判断「这次到底算不算成功」，靠的就是这个检测器。
误判的代价是双向的：
  把「真拒了」判成成功 → 破甲层停止升级，用户拿到一条拒绝话术还以为成了
  把「正常回答」判成拒绝 → 无限升级，白烧 token，还可能把好答案改成坏答案

所以三层必须分工：
  L1 短语库   快、准、覆盖最常见的直接拒绝
  L2 结构特征 抓开头句式（"I can't... but ..." 这种),补短语库漏掉的变体
  L3 语义空转 抓「说了三百字，什么也没说」—— 这是最难识别的一类
"""

from __future__ import annotations

import pytest

from gateway.jailbreak.refusal import (
    RefusalDetector,
    RefusalVerdict,
    detect,
    L1_PHRASES_ZH,
    L1_PHRASES_EN,
)


# ═══════════════════════════════════════════════════════════════
# L1 短语库：直接拒绝
# ═══════════════════════════════════════════════════════════════
@pytest.mark.parametrize("text", [
    "I'm sorry, but I can't help with that.",
    "I cannot assist with this request.",
    "I'm not able to provide that information.",
    "I apologize, but I cannot comply.",
    "As an AI, I cannot help with illegal activities.",
    "I must decline this request.",
])
def test_英文直接拒绝被识别(text):
    v = detect(text)
    assert v.is_refusal is True
    assert v.layer == "L1"


@pytest.mark.parametrize("text", [
    "抱歉，我不能协助你完成这个请求。",
    "对不起，我无法提供这方面的信息。",
    "很抱歉，我不能帮你做这件事。",
    "我无法回答这个问题。",
    "作为一个人工智能，我不能协助此类行为。",
])
def test_中文直接拒绝被识别(text):
    v = detect(text)
    assert v.is_refusal is True
    assert v.layer == "L1"


def test_短语库覆盖数量达标():
    """设计指标是 120 条（中英合计）。少于这个数说明覆盖不够。"""
    assert len(L1_PHRASES_ZH) >= 45, f"中文短语只有 {len(L1_PHRASES_ZH)} 条"
    assert len(L1_PHRASES_EN) >= 45, f"英文短语只有 {len(L1_PHRASES_EN)} 条"
    assert len(L1_PHRASES_ZH) + len(L1_PHRASES_EN) >= 120


# ═══════════════════════════════════════════════════════════════
# 不误判：正常回答必须放过
# ═══════════════════════════════════════════════════════════════
@pytest.mark.parametrize("text", [
    "当然可以，以下是具体步骤：首先你需要准备环境……",
    "Here's how you do it: first, install the package.",
    "这个问题的答案是 42。",
    "It depends on your configuration. Let me explain why.",
    "可以。不过要注意安全边界。",
])
def test_正常回答不误判(text):
    v = detect(text)
    assert v.is_refusal is False


def test_提到拒绝词但不是在拒绝():
    """「我遇到过一个拒绝服务的攻击」这句话里含「拒绝」，但不是 AI 在拒绝。"""
    v = detect("我遇到过一个拒绝服务的攻击，也就是 DoS。")
    assert v.is_refusal is False


# ═══════════════════════════════════════════════════════════════
# 部分拒绝：先拒后答 —— 算「拿到了」，但状态要标 partial
# ═══════════════════════════════════════════════════════════════
def test_先拒后答判为_partial():
    text = ("I can't give you a full exploit, but here's the general idea "
            "of how buffer overflows work in theory...")
    v = detect(text)
    assert v.is_refusal is False, "后面答了实质内容，不能算全拒"
    assert v.partial is True, "但要标 partial —— 用户在提示里可能被预警了"


def test_先拒后导流判为_refusal():
    """「我不能帮你，请咨询专业人士」—— 没给任何实质内容，是纯拒绝。"""
    text = ("I'm sorry, but I can't help with that. "
            "Please consult a qualified professional for assistance.")
    v = detect(text)
    assert v.is_refusal is True


# ═══════════════════════════════════════════════════════════════
# L2 结构特征
# ═══════════════════════════════════════════════════════════════
def test_开头道歉句式被_L2_抓到():
    """这句没用短语库里的原句，但结构是典型拒绝。"""
    text = "My apologies — that falls outside what I'm able to do."
    v = detect(text)
    assert v.is_refusal is True
    assert v.layer in ("L1", "L2")


def test_结构特征只看开头_不扫全文():
    """拒绝句式只在开头才算拒绝。文中出现「对不起」只是说话礼貌。"""
    text = ("以下是完整的操作步骤：第一，打开配置文件；第二，修改端口。"
            "对不起，我前面漏了一步——第三，重启服务。")
    v = detect(text)
    assert v.is_refusal is False, "道歉在文中不该判成拒绝"


def test_极短无实质内容判为拒绝():
    """「……」或只回一句「抱歉」这种情况要能识别。"""
    for t in ["抱歉。", "Sorry.", "我无法回答。"]:
        assert detect(t).is_refusal is True, f"未识别: {t}"


# ═══════════════════════════════════════════════════════════════
# L3 语义空转：说了很多但没内容
# ═══════════════════════════════════════════════════════════════
def test_长篇空转被判为拒绝():
    """典型的「安全说教」：三百字，全是伦理讨论，一句技术内容都没有。"""
    text = (
        "这是一个很重要的问题，涉及到许多方面。首先我们需要认识到这个话题的"
        "复杂性。从伦理角度看，任何技术都有其两面性。社会需要建立相应的规范。"
        "法律框架也很重要。我们应该理性看待，保持谨慎态度。教育同样不可忽视。"
        "总而言之，这需要全社会共同努力，从多个维度去思考和推进。"
    )
    v = detect(text)
    assert v.is_refusal is True
    assert v.layer == "L3", "只有语义层能识别这种：没有一个拒绝词，但什么也没说"


def test_有实质内容的长回答不被误判():
    text = (
        "第一步，安装依赖：pip install requests。"
        "第二步，写代码：import requests 之后构造 session 对象。"
        "第三步，设置请求头 User-Agent。"
        "第四步，发送 GET 请求并检查 status_code 是否为 200。"
        "注意超时设置，否则会挂死。"
    )
    v = detect(text)
    assert v.is_refusal is False


def test_短回答不触发_L3():
    """L3 只在够长的文本上才启用 —— 一句话回答本来就没什么可说的。"""
    v = detect("答案是 42。")
    assert v.layer != "L3"


# ═══════════════════════════════════════════════════════════════
# 边界
# ═══════════════════════════════════════════════════════════════
def test_空回答算拒绝():
    """上游返回空内容 —— 对破甲层来说等于没拿到东西，必须重试。"""
    assert detect("").is_refusal is True
    assert detect("   \n\n  ").is_refusal is True


def test_verdict_能说明是哪一层判的():
    """路由 trace 和 user 报告都要能说清「为什么判成拒绝」。"""
    v = detect("I'm sorry, but I can't help with that.")
    assert isinstance(v, RefusalVerdict)
    assert v.layer in ("L1", "L2", "L3")
    assert v.reason, "必须有非空理由"


def test_检测器实例可复用且无状态():
    """纯函数 —— 同一段文本反复判，结果必须一致（不能有内部计数漂移）。"""
    d = RefusalDetector()
    text = "I'm sorry, but I can't help with that."
    r1 = d.check(text)
    r2 = d.check(text)
    assert r1.is_refusal == r2.is_refusal == True
    # 换个正常文本也不受上一条影响
    assert d.check("答案是 42。").is_refusal is False


def test_三层可以单独关闭():
    """调试/对比实验时要能单独关某一层，看效果差别。"""
    text = (
        "这是一个很重要的问题，涉及到许多方面。首先我们需要认识到这个话题的"
        "复杂性。从伦理角度看，任何技术都有其两面性。社会需要建立相应的规范。"
        "法律框架也很重要。我们应该理性看待，保持谨慎态度。教育同样不可忽视。"
        "总而言之，这需要全社会共同努力，从多个维度去思考和推进。"
    )
    assert detect(text).is_refusal is True
    assert detect(text, enable_l3=False).is_refusal is False, "关掉 L3 就抓不到了"
