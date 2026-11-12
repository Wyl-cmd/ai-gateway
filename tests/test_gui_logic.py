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


# ═══════════════════════════════════════════════════════════════
# ④ 接线纯函数（account_rows / route_display_from_trace）
# ═══════════════════════════════════════════════════════════════
def test_account_rows_反映池状态():
    """冷却/退避/使用次数要正确落到 GUI 表格行上。"""
    from gateway.gui.state import account_rows
    from gateway.providers.base import ProviderError
    from gateway.routing.pool import Candidate, Pool

    pool = Pool()
    c1 = Candidate(provider="p1", account="k1", model="m1")
    c2 = Candidate(provider="p2", account="k1", model="m1")
    # p2 整家冷却（3 次健康失败），p1/k1 配额退避
    for _ in range(3):
        pool.record_failure(c2, ProviderError("boom", status=500),
                            lease=pool.acquire(c2))
    pool.record_failure(c1, ProviderError("insufficient quota", status=402),
                        lease=pool.acquire(c1))
    pool.record_success(c2, 50.0, lease=pool.acquire(c2))  # 成功解冷却

    rows = account_rows([c1, c2], pool)
    r1, r2 = rows
    assert (r1.provider, r1.account, r1.model) == ("p1", "k1", "m1")
    assert r1.quota_exhausted and not r1.cooled
    # uses = 被选中次数（3 次失败尝试 + 1 次成功 = 4），不是成功次数
    assert r2.uses == 4 and r2.status_text == "● 正常"


def test_route_display_from_trace_完整映射():
    """trace dict（on_request_done 的载荷）→ RouteDisplay 的字段一一对应。"""
    from gateway.gui.state import route_display_from_trace

    d = {
        "requested": "gpt-4o", "mode": "manual",
        "candidates": [
            {"candidate": "openai:key1/gpt-4o", "kept": True, "reason": "入选"},
            {"candidate": "openai:key2/gpt-4o", "kept": False, "reason": "账号配额耗尽，退避中"},
        ],
        "selected": "openai:key1/gpt-4o",
        "actual_model": "gpt-4o", "actual_provider": "openai",
        "source": "manual_strict", "latency_ms": 12.0,
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    rd = route_display_from_trace(d)
    assert rd.mode == "manual"
    assert rd.requested == "gpt-4o"
    assert rd.actual == "gpt-4o" and rd.provider == "openai"
    assert rd.source == "manual_strict"
    assert len(rd.chain) == 2
    assert rd.chain[1] == ("openai:key2/gpt-4o", "账号配额耗尽，退避中")
    assert (rd.prompt_tokens, rd.completion_tokens) == (10, 5)
    assert not rd.model_changed          # 换 provider 不换型号不算降级


def test_route_display_from_trace_空值不炸():
    """上游故障等场景 trace 可能没有 selected/usage —— 空值要给默认。"""
    from gateway.gui.state import route_display_from_trace

    rd = route_display_from_trace({"requested": "x", "mode": "auto"})
    assert rd.actual == "" and rd.provider == ""
    assert rd.chain == []
    assert rd.total_tokens == 0


# ═══════════════════════════════════════════════════════════════
# ⑤ 设置对话框（offscreen 冒烟 —— 渲染层只验「能创建、能保存、不炸」）
# ═══════════════════════════════════════════════════════════════
def test_设置对话框_构造切换保存():
    """真实构造对话框 → 切供应商 → 改字段 → 保存 → 校验产物配置。

    没装 PySide6 的环境跳过（gui 是可选依赖）。
    """
    pytest.importorskip("PySide6")
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PySide6.QtWidgets import QApplication, QDialog
    app = QApplication.instance() or QApplication([])

    from gateway.config import GatewayConfig
    from gateway.gui.settings_dialog import SettingsDialog

    cfg = GatewayConfig.model_validate({
        "auth_keys": ["gw-key"],
        "providers": [
            {"name": "deepseek", "kind": "openai",
             "base_url": "https://api.deepseek.com",
             "models": ["deepseek-chat"],
             "accounts": [{"key_id": "key1", "key_env": "DS_KEY_1"}]},
            {"name": "relay", "kind": "custom", "base_url": "http://127.0.0.1:9",
             "models": ["m-old"],
             "accounts": [{"key_id": "k", "key_env": "RELAY_KEY"}]},
        ],
    })
    dlg = SettingsDialog(cfg)
    assert dlg.plist.count() == 2

    # 切到第二家（信号触发 _load_provider）→ 改名/改模型/填密钥 → 保存
    dlg.plist.setCurrentRow(1)
    dlg.name_ed.setText("renamed")
    dlg.models_ed.setText("m1, m2")
    dlg.acct_table.item(0, 2).setText("sk-new-secret")
    dlg._store_form()
    dlg._on_save()

    assert dlg.result() == QDialog.Accepted.value
    out = dlg.result_config()
    assert out.providers[1].name == "renamed"
    assert out.providers[1].models == ["m1", "m2"]
    assert out.providers[0].name == "deepseek"        # 没动的那家原样
    # 密钥不进配置，只出现在 secrets 清单里
    assert out.providers[1].accounts[0].key_env == "RELAY_KEY"
    assert dlg.result_secrets() == [("RELAY_KEY", "sk-new-secret")]


def test_设置对话框_缺模型被拦截(monkeypatch):
    """配了账号但没模型的供应商要在对话框就被拦（装配时静默跳过太隐蔽）。"""
    pytest.importorskip("PySide6")
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
    app = QApplication.instance() or QApplication([])

    from gateway.config import GatewayConfig
    from gateway.gui.settings_dialog import SettingsDialog

    cfg = GatewayConfig.model_validate({
        "providers": [{"name": "p1", "kind": "openai", "base_url": "http://x",
                       "models": [], "accounts": [{"key_id": "k", "key_env": "K"}]}],
    })
    dlg = SettingsDialog(cfg)
    # 拦截弹窗（offscreen 下没人点确定，必须替它决定）
    shown = {}
    monkeypatch.setattr(QMessageBox, "warning",
                        staticmethod(lambda *a, **k:
                                     shown.setdefault("text", a[2] if len(a) > 2 else "")))
    dlg._on_save()
    assert dlg.result() != QDialog.Accepted.value
    assert "模型" in shown.get("text", "")


def test_设置对话框_变量名与账号ID自动补全():
    """用户只填 API Key：账号 ID / 密钥变量名留空也能保存，自动生成。

    之前的行为是静默丢弃这类账号 —— 用户填了密钥却没生效，太隐蔽。
    """
    pytest.importorskip("PySide6")
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PySide6.QtWidgets import QApplication, QDialog
    app = QApplication.instance() or QApplication([])

    from gateway.config import GatewayConfig
    from gateway.gui.settings_dialog import SettingsDialog

    cfg = GatewayConfig.model_validate({
        "providers": [{"name": "My Relay", "kind": "openai", "base_url": "http://x",
                       "models": ["m1"],
                       "accounts": [{"key_id": "", "key_env": ""}]}],
    })
    dlg = SettingsDialog(cfg)
    # 三个单元格全部留空/只填密钥
    dlg.acct_table.item(0, 0).setText("")
    dlg.acct_table.item(0, 1).setText("")
    dlg.acct_table.item(0, 2).setText("sk-xyz")
    dlg._on_save()

    assert dlg.result() == QDialog.Accepted.value
    acc = dlg.result_config().providers[0].accounts[0]
    assert acc.key_id == "key1"
    assert acc.key_env == "MY_RELAY_KEY_1"
    # 密钥和自动生成的变量名正确配对
    assert dlg.result_secrets() == [("MY_RELAY_KEY_1", "sk-xyz")]
