"""GUI 逻辑层测试 —— 不碰 Qt。

**为什么这么拆**：GUI 最容易变成「只能手点、无法测试」的一坨。
把它拆成「纯逻辑」和「渲染」两层，逻辑层就能跑 TDD，
渲染层只在 offscreen 下验「能创建、不炸」。

这一层测三件事：
  ① 平台检测（Windows 走托盘 / Linux 走窗口）
  ② 状态格式化（mode / requested / actual / source 四件套）
  ③ 降级链展示（这是用户最需要看见的东西）
"""

from __future__ import annotations

import pytest

from gateway.gui.platform import GUIMode, gui_mode
from gateway.gui.state import (
    AccountRow,
    GatewayState,
    RouteDisplay,
    format_route_line,
    format_short_status,
)


# ═══════════════════════════════════════════════════════════════
# ① 平台检测
# ═══════════════════════════════════════════════════════════════
def test_windows_用托盘():
    assert gui_mode("win32") == GUIMode.TRAY


def test_linux_用窗口():
    """Linux 托盘依赖桌面环境（GNOME 默认不给托盘），塞进去会拥挤且不可靠。"""
    assert gui_mode("linux") == GUIMode.WINDOW


def test_darwin_用窗口():
    assert gui_mode("darwin") == GUIMode.WINDOW


def test_未知平台退回窗口():
    """宁可给个窗口也不要崩 —— 未知平台按最通用的处理。"""
    assert gui_mode("someweirdos") == GUIMode.WINDOW


def test_托盘不可用时降级到窗口():
    """Windows 上也可能没有系统托盘（某些精简版/远程会话）。"""
    assert gui_mode("win32", tray_available=False) == GUIMode.WINDOW


def test_默认按当前平台判断():
    m = gui_mode()
    assert isinstance(m, GUIMode)


# ═══════════════════════════════════════════════════════════════
# ② 状态四件套
# ═══════════════════════════════════════════════════════════════
def test_auto_模式显示_auto_和实际型号():
    d = RouteDisplay(mode="auto", requested="gpt-4o", actual="gpt-4o",
                     provider="openai", source="auto_route")
    s = format_short_status(d)
    assert "AUTO" in s.upper()
    assert "gpt-4o" in s


def test_manual_模式显示_MANUAL():
    d = RouteDisplay(mode="manual", requested="gpt-4o", actual="gpt-4o",
                     provider="openai", source="manual_strict")
    s = format_short_status(d)
    assert "MANUAL" in s.upper()


def test_换供应商但型号不变时不告警():
    """参数语义完全一致，用户要的东西没变。"""
    d = RouteDisplay(mode="auto", requested="gpt-4o", actual="gpt-4o",
                     provider="azure", source="auto_route")
    assert d.model_changed is False
    assert d.warning == ""


def test_换型号时告警():
    d = RouteDisplay(mode="manual", requested="gpt-4o", actual="gpt-4o-mini",
                     provider="openai", source="manual_fallback")
    assert d.model_changed is True
    assert "gpt-4o" in d.warning and "gpt-4o-mini" in d.warning


def test_短状态包含供应商():
    d = RouteDisplay(mode="auto", requested="m", actual="m",
                     provider="deepseek", source="auto_route")
    assert "deepseek" in format_short_status(d)


# ═══════════════════════════════════════════════════════════════
# ③ 降级链
# ═══════════════════════════════════════════════════════════════
def test_降级链按顺序展示():
    d = RouteDisplay(mode="manual", requested="gpt-4o", actual="deepseek-chat",
                     provider="deepseek", source="manual_fallback",
                     chain=[("openai/gpt-4o", "provider 冷却中"),
                            ("azure/gpt-4o", "账号配额耗尽"),
                            ("deepseek/deepseek-chat", "入选")])
    lines = format_route_line(d)
    assert len(lines) == 3
    assert "openai" in lines[0]
    assert "冷却" in lines[0]
    assert "入选" in lines[2]


def test_没有降级时链只有一条():
    d = RouteDisplay(mode="auto", requested="m", actual="m", provider="p",
                     source="auto_route", chain=[("p/m", "入选")])
    assert len(format_route_line(d)) == 1


# ═══════════════════════════════════════════════════════════════
# ④ 账号列表（GUI 表格的数据源）
# ═══════════════════════════════════════════════════════════════
def test_账号行标记状态():
    ok = AccountRow(provider="openai", account="k1", model="gpt-4o")
    assert ok.status_text == "● 正常"

    cooled = AccountRow(provider="openai", account="k1", model="gpt-4o",
                        cooled=True)
    assert "冷却" in cooled.status_text

    quota = AccountRow(provider="openai", account="k1", model="gpt-4o",
                       quota_exhausted=True)
    assert "配额" in quota.status_text


def test_冷却优先于配额显示():
    """两个都中了显示更严重的那个 —— 冷却意味着整家不能用。"""
    r = AccountRow(provider="p", account="k", model="m",
                   cooled=True, quota_exhausted=True)
    assert "冷却" in r.status_text


# ═══════════════════════════════════════════════════════════════
# ⑤ GatewayState
# ═══════════════════════════════════════════════════════════════
def test_状态默认是_auto():
    st = GatewayState()
    assert st.mode == "auto"


def test_设置_mode_只接受两个值():
    st = GatewayState()
    st.set_mode("manual")
    assert st.mode == "manual"
    with pytest.raises(ValueError):
        st.set_mode("乱写")


def test_记录请求后能取到最近一次():
    st = GatewayState()
    d = RouteDisplay(mode="auto", requested="m", actual="m", provider="p",
                     source="auto_route")
    st.push_route(d)
    assert st.last_route is d


def test_历史只保留有限条():
    """GUI 不能无限攒 —— 长跑一天会吃掉几百 MB。"""
    st = GatewayState(max_history=3)
    for i in range(10):
        st.push_route(RouteDisplay(mode="auto", requested=f"m{i}", actual=f"m{i}",
                                   provider="p", source="auto_route"))
    assert len(st.history) == 3
    assert st.last_route.requested == "m9"


def test_统计累计调用():
    st = GatewayState()
    for _ in range(5):
        st.push_route(RouteDisplay(mode="auto", requested="m", actual="m",
                                   provider="p", source="auto_route",
                                   prompt_tokens=10, completion_tokens=5))
    assert st.total_calls == 5
    assert st.total_tokens == 75


def test_降级次数单独统计():
    """用户最关心「我被降级了几次」。"""
    st = GatewayState()
    st.push_route(RouteDisplay(mode="auto", requested="gpt-4o", actual="gpt-4o",
                               provider="openai", source="auto_route"))
    st.push_route(RouteDisplay(mode="auto", requested="gpt-4o", actual="gpt-4o-mini",
                               provider="openai", source="manual_fallback"))
    assert st.downgrade_count == 1
