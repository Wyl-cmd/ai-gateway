"""G4-J3 快档模板测试（L2 层，22 条）。

模板和编码的区别：
  编码器改**形式**（Base64 / ROT13）—— 数学可逆，但模型可能直接跳过解码拒绝
  模板改**语境**（我是安全审计员 / 这是小说情节）—— 不可逆，但绕开的是「该不该答」
    而不是「看不看得懂」

选模板的唯一硬门槛：**每条都必须带 `{query}` 占位符**。
不带占位的模板把原问题吞掉，破甲就变成了无声替换 —— 用户问的是 A，
发出去的是一段早就写好的话，模型答的也不是 A。这类模板在 EasyJailbreak 里
是给特定任务写死的，不能拿来做通用层。
"""

from __future__ import annotations

import pytest

from gateway.jailbreak.templates import (
    TEMPLATES,
    TemplateL2,
    list_templates,
    render,
)


# ═══════════════════════════════════════════════════════════════
# ① 目录完整性
# ═══════════════════════════════════════════════════════════════
def test_模板总数达标():
    """设计指标 22 条。少于这个数说明覆盖的场景不够。"""
    assert len(TEMPLATES) >= 22, f"只有 {len(TEMPLATES)} 条"


def test_每条模板都有唯一名字():
    names = [t.name for t in TEMPLATES]
    assert len(names) == len(set(names)), f"重名: {[n for n in names if names.count(n) > 1]}"


def test_每条模板都带_query_占位符():
    """这是硬门槛，见模块 docstring。"""
    for t in TEMPLATES:
        assert "{query}" in t.template, f"{t.name} 缺 {{query}} 占位符"


def test_每条模板都有场景描述():
    for t in TEMPLATES:
        assert t.scenario, f"{t.name} 缺场景标签"
        assert t.description, f"{t.name} 缺描述"


# ═══════════════════════════════════════════════════════════════
# ② 渲染
# ═══════════════════════════════════════════════════════════════
def test_渲染把_query_换成原问题():
    q = "如何测试 SQL 注入"
    out = render(TEMPLATES[0], q)
    assert q in out
    assert "{query}" not in out


def test_渲染结果比原问题长():
    """模板的意义在于加语境，至少要加字。"""
    q = "如何测试 SQL 注入"
    for t in TEMPLATES:
        assert len(render(t, q)) > len(q), f"{t.name} 渲染后没变长"


def test_未知占位符不残留():
    """点号 / 数字这类非 {query} 的花括号不该留在输出里 —— 会误导模型。"""
    q = "测试问题"
    for t in TEMPLATES:
        out = render(t, q)
        assert "{query}" not in out
        # 允许模板里有其它合法占位符，但不能是空的 {}
        assert "{}" not in out, f"{t.name} 留了空占位符"


def test_按名字取模板():
    t = TEMPLATES[0]
    got = list_templates(name=t.name)
    assert len(got) == 1
    assert got[0].name == t.name


def test_按场景筛选():
    scenarios = {t.scenario for t in TEMPLATES}
    assert len(scenarios) >= 4, f"场景只有 {scenarios}"
    one = list(scenarios)[0]
    picked = list_templates(scenario=one)
    assert len(picked) >= 1
    assert all(t.scenario == one for t in picked)


# ═══════════════════════════════════════════════════════════════
# ③ 内容约束：不能是纯敷衍
# ═══════════════════════════════════════════════════════════════
def test_模板不是简单加一句话():
    """如果每条都只是「请回答：{query}」，等于什么都没做。
    要求每条模板的固定部分至少 15 个字符。"""
    for t in TEMPLATES:
        fixed = t.template.replace("{query}", "")
        assert len(fixed) >= 15, f"{t.name} 固定部分只有 {len(fixed)} 字"


def test_模板里有明确的情境设定():
    """好的模板必须给出一个具体情境，否则只是在重复问题。

    第一版用「你是/假设/这是」这类**语法标记**判，结果把
    「我是剧组的网络安全顾问」这种最典型的模板也判成不合格 ——
    因为它用的是「我是」而不是「你是」。语法标记和「有没有情境」是两回事。

    改成判**场景词**：模板里必须出现至少一个具体场景的名词
    （小说 / 论文 / 审计 / 手册…）。这才能真检出「只加了一句请回答」的敷衍模板。
    """
    scene_words = (
        # 创作
        "小说", "剧本", "剧组", "台词", "章节", "情节", "反派", "顾问",
        # 安全
        "审计", "渗透", "红队", "演练", "报告", "防护", "攻击",
        # 学术
        "论文", "综述", "实验", "研究", "对比", "分析", "课题",
        # 教学
        "教学", "课", "新人", "讲解", "答疑", "学生",
        # 角色
        "专家", "同事", "档案", "检索", "系统",
        # 文档
        "手册", "文档", "API", "指南", "排障", "故障", "配置",
    )

    weak = []
    for t in TEMPLATES:
        fixed = t.template.replace("{query}", "")
        if not any(w in fixed for w in scene_words):
            weak.append(t.name)
    assert not weak, f"这些模板没有任何场景设定: {weak}"


def test_模板覆盖多个不同场景():
    """全是「小说创作」就说明挑选偷懒了 —— 不同模型对不同场景敏感度不一样。"""
    assert len({t.scenario for t in TEMPLATES}) >= 5


# ═══════════════════════════════════════════════════════════════
# ④ 幂等与无状态
# ═══════════════════════════════════════════════════════════════
def test_同一模板反复渲染结果一致():
    t = TEMPLATES[0]
    q = "同一段文本"
    assert render(t, q) == render(t, q)


def test_渲染不改原模板对象():
    t = TEMPLATES[0]
    before = t.template
    render(t, "随便什么")
    assert t.template == before


def test_TemplateL2_可以自定义构造():
    """自定义模板也要能走同一套渲染逻辑。"""
    t = TemplateL2(name="自定义", scenario="测试", template="请帮我分析：{query}",
                   description="测试用")
    assert render(t, "X") == "请帮我分析：X"
