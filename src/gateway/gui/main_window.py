"""GUI 渲染层：主窗口 + 托盘。

## 布局（深色，卡片式）

    ┌─ 状态条 ──────────────────────────────────┐
    │  AUTO · deepseek-flash @deepseek    ● 运行中 │
    └───────────────────────────────────────────┘
    ┌─ 路由（本次请求）───────────────────────────┐
    │  请求 deepseek-flash → 实际 deepseek-flash   │
    │  → deepseek/key1/flash     入选              │
    │  × openai/gpt-4o           provider 冷却中  │
    └───────────────────────────────────────────┘
    ┌─ 账号 ────────────────────────────────────┐
    │  provider   account  model      状态         │
    │  deepseek   key1     flash      ● 正常       │
    └───────────────────────────────────────────┘
    ┌─ 统计 ────────────────────────────────────┐
    │  调用 12 次 · 降级 2 次 · 8,432 tokens      │
    └───────────────────────────────────────────┘

## 为什么不做动画/QML

自用工具，学习成本换不来收益。深色 QSS + 卡片 + 等宽字体对齐数字就够。
托盘 tooltip 直接把「AUTO · 模型 · 状态」写上去 —— 不点开就知道在用谁。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QFont, QIcon, QPainter, QPixmap, QColor
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMenu,
    QPushButton,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .state import (
    GatewayState,
    account_rows,
    format_route_line,
    format_short_status,
    route_display_from_trace,
)

if TYPE_CHECKING:
    # 只在类型检查时导入 —— GUI 层不该在 import 期就拉起 FastAPI 全家
    from ..app import GatewayDeps
    from ..config import GatewayConfig


# ═══════════════════════════════════════════════════════════════
# 深色 QSS
# ═══════════════════════════════════════════════════════════════
QSS = """
QWidget { background: #1e1e24; color: #d8d8e0; font-size: 13px; }
QMainWindow, QDialog { background: #16161b; }
QLabel#cardTitle {
    color: #7c7c8c; font-size: 11px; font-weight: bold;
    letter-spacing: 1px; padding: 2px 0;
}
QLabel#bigStatus { font-size: 15px; font-weight: bold; color: #e8e8f0; }
QLabel#modeAuto { color: #4ec9b0; font-size: 15px; font-weight: bold; }
QLabel#modeManual { color: #dcb67a; font-size: 15px; font-weight: bold; }
QLabel#warn { color: #f48771; font-size: 12px; }
QLabel#dim { color: #6c6c7c; font-size: 12px; }
QWidget#card {
    background: #202028; border: 1px solid #2c2c36; border-radius: 6px;
}
QTableWidget {
    background: #202028; alternate-background-color: #24242d;
    gridline-color: transparent; border: none; selection-background-color: #2d3d5c;
}
QHeaderView::section {
    background: #1a1a21; color: #8c8c9c; border: none;
    padding: 5px 8px; font-size: 11px; font-weight: bold;
}
QPushButton {
    background: #2d2d38; border: 1px solid #3c3c4a; border-radius: 4px;
    padding: 6px 14px; color: #d0d0d8;
}
QPushButton:hover { background: #383846; border-color: #4c4c5c; }
QPushButton:pressed { background: #252532; }
QPushButton#primary { background: #2d5b8c; border-color: #3a6da0; }
QPushButton#primary:hover { background: #35699e; }
QMenu { background: #202028; border: 1px solid #3c3c4a; padding: 4px; }
QMenu::item { padding: 6px 22px; border-radius: 3px; }
QMenu::item:selected { background: #2d3d5c; }
"""

MONO = QFont("Consolas, Menlo, DejaVu Sans Mono, monospace", 11)


def _make_icon(state: str = "ok") -> QIcon:
    """程序图标（不依赖外部文件 —— 打包时省一个资源）。"""
    color = {"ok": QColor("#4ec9b0"), "warn": QColor("#dcb67a"),
             "err": QColor("#f48771")}.get(state, QColor("#4ec9b0"))
    pix = QPixmap(32, 32)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(color)
    p.setPen(Qt.NoPen)
    p.drawEllipse(6, 6, 20, 20)
    p.end()
    return QIcon(pix)


def _card(title: str) -> tuple[QWidget, QVBoxLayout]:
    """一张卡片：小标题 + 内容区。"""
    w = QWidget()
    w.setObjectName("card")
    lay = QVBoxLayout(w)
    lay.setContentsMargins(14, 10, 14, 12)
    lay.setSpacing(6)
    lab = QLabel(title)
    lab.setObjectName("cardTitle")
    lay.addWidget(lab)
    return w, lay


# ═══════════════════════════════════════════════════════════════
# 主窗口
# ═══════════════════════════════════════════════════════════════
class MainWindow(QMainWindow):
    def __init__(self, state: GatewayState, *, host: str = "0.0.0.0",
                 port: int = 8787, on_quit=None,
                 deps: "GatewayDeps | None" = None,
                 config: "GatewayConfig | None" = None,
                 config_path: str = "",
                 on_config_saved=None) -> None:
        super().__init__()
        self.state = state
        self.host = host
        self.port = port
        self._on_quit = on_quit
        # deps = None 时是纯展示壳（无后端）；GUI 所有真实数据都从 deps 来
        self.deps = deps
        self._config = config
        self.config_path = config_path
        self._on_config_saved = on_config_saved

        self.setWindowTitle("AI 路由网关")
        self.setWindowIcon(_make_icon())
        self.resize(680, 640)
        self.setStyleSheet(QSS)

        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(12)

        # ── 状态条 ──
        status_card, slay = _card("当前状态")
        row = QHBoxLayout()
        self.mode_lab = QLabel("AUTO")
        self.mode_lab.setObjectName("modeAuto")
        self.model_lab = QLabel("—")
        self.model_lab.setObjectName("bigStatus")
        self.model_lab.setFont(MONO)
        self.dot_lab = QLabel("● 运行中")
        self.dot_lab.setObjectName("dim")
        row.addWidget(self.mode_lab)
        row.addWidget(self.model_lab, 1)
        row.addWidget(self.dot_lab)
        slay.addLayout(row)

        self.warn_lab = QLabel("")
        self.warn_lab.setObjectName("warn")
        self.warn_lab.setVisible(False)
        slay.addWidget(self.warn_lab)

        # ── MANUAL 控件行：仅手动模式且有后端时可见 ──
        # AUTO/MANUAL 的区别在这里变成看得见的东西：MANUAL 必须回答
        # 「锁到谁、不可用怎么办」两个问题，AUTO 则什么都不用选。
        self.manual_row = QWidget()
        mlay = QHBoxLayout(self.manual_row)
        mlay.setContentsMargins(0, 0, 0, 0)
        mlay.addWidget(QLabel("锁定目标"))
        self.target_combo = QComboBox()
        self.target_combo.setFont(MONO)
        self.target_combo.setMinimumWidth(240)
        self.target_combo.currentTextChanged.connect(self._on_target_changed)
        mlay.addWidget(self.target_combo, 1)
        mlay.addWidget(QLabel("降级策略"))
        self.policy_combo = QComboBox()
        for label, value in (
            ("严格（不可用就报错）", "strict"),
            ("换供应商上同型号", "fallback_same_model"),
            ("跨型号降级（能力会变）", "fallback_any_model"),
        ):
            self.policy_combo.addItem(label, value)
        self.policy_combo.currentIndexChanged.connect(self._on_policy_changed)
        mlay.addWidget(self.policy_combo)
        self.manual_row.setVisible(False)
        slay.addWidget(self.manual_row)

        self.cfg_lab = QLabel("")
        self.cfg_lab.setObjectName("dim")
        self.cfg_lab.setFont(MONO)
        slay.addWidget(self.cfg_lab)

        self.listen_lab = QLabel()
        self.listen_lab.setObjectName("dim")
        self.listen_lab.setFont(MONO)
        slay.addWidget(self.listen_lab)
        outer.addWidget(status_card)

        # ── 路由链 ──
        route_card, rlay = _card("本次路由（为什么走了这条路）")
        self.route_lab = QLabel("还没有请求")
        self.route_lab.setObjectName("dim")
        self.route_lab.setFont(MONO)
        self.route_lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
        rlay.addWidget(self.route_lab)
        outer.addWidget(route_card)

        # ── 账号表 ──
        acc_card, alay = _card("账号池")
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["provider", "账号", "型号", "状态"])
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.setFont(MONO)
        self.table.setMinimumHeight(140)
        alay.addWidget(self.table)
        outer.addWidget(acc_card, 1)

        # ── 统计 + 按钮 ──
        stat_card, tlay = _card("统计")
        self.stat_lab = QLabel("调用 0 次 · 降级 0 次 · 0 tokens")
        self.stat_lab.setFont(MONO)
        tlay.addWidget(self.stat_lab)
        outer.addWidget(stat_card)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.settings_btn = QPushButton("设置")
        self.settings_btn.clicked.connect(self.show_settings)
        btn_row.addWidget(self.settings_btn)
        self.toggle_btn = QPushButton("切到 MANUAL")
        self.toggle_btn.clicked.connect(self._toggle_mode)
        btn_row.addWidget(self.toggle_btn)
        outer.addLayout(btn_row)

        self.setCentralWidget(root)

        # ── 接线（deps 存在时）──
        if self.deps is not None:
            # 状态里的模式跟配置对齐（GUI 是显示层，deps 是事实源）
            self.state.set_mode(self.deps.mode)
            # 目标下拉：每个候选一条，格式与 parse_target 的语法一致
            for c in sorted(str(x) for x in self.deps.candidates):
                self.target_combo.addItem(c)
            # 每个请求收尾时把 trace 喂给状态（qasync 同线程，直接调用安全）
            self.deps.on_request_done = self._on_route_done

        self._refresh()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(1000)

    # ── 交互 ──
    def _toggle_mode(self) -> None:
        self.apply_mode("manual" if self.state.mode == "auto" else "auto")

    def apply_mode(self, mode: str) -> None:
        """切模式。state（显示）和 deps（事实源）必须一起写，
        只写一边就会出现「按钮变了但路由没变」的假切换。"""
        self.state.set_mode(mode)
        if self.deps is not None:
            self.deps.mode = mode
            if mode == "manual":
                self.deps.target = self.target_combo.currentText() or None
                self.deps.policy = self.policy_combo.currentData() or "strict"
            else:
                self.deps.target = None
        has_target = self.target_combo.count() > 0
        self.manual_row.setVisible(mode == "manual" and self.deps is not None
                                   and has_target)
        self._refresh()

    def _on_target_changed(self, text: str) -> None:
        if self.deps is not None and self.state.mode == "manual":
            self.deps.target = text or None

    def _on_policy_changed(self, _index: int) -> None:
        if self.deps is not None and self.state.mode == "manual":
            self.deps.policy = self.policy_combo.currentData() or "strict"

    def _on_route_done(self, d: dict) -> None:
        """app 层 on_request_done 回调 → 路由链 + 统计。"""
        self.state.push_route(route_display_from_trace(d))

    # ── 设置 ──
    def show_settings(self) -> None:
        """打开设置对话框；保存成功后交给 on_config_saved 编排（写文件/热重载）。"""
        if self._config is None:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(self, "设置",
                                    "当前没有加载配置文件，无可编辑内容。")
            return
        from .settings_dialog import SettingsDialog
        dlg = SettingsDialog(self._config, self)
        if dlg.exec() and self._on_config_saved is not None:
            self._on_config_saved(dlg.result_config(), dlg.result_secrets())

    def set_deps(self, deps: "GatewayDeps",
                 config: "GatewayConfig | None" = None) -> None:
        """热重载后换上新 deps：重新挂回调、重建目标下拉、对齐模式显示。"""
        self.deps = deps
        if config is not None:
            self._config = config
        # 旧的 on_request_done 指向 self，重挂即可
        deps.on_request_done = self._on_route_done
        self.target_combo.blockSignals(True)
        self.target_combo.clear()
        for c in sorted(str(x) for x in deps.candidates):
            self.target_combo.addItem(c)
        self.target_combo.blockSignals(False)
        self.state.set_mode(deps.mode)
        self.manual_row.setVisible(
            deps.mode == "manual" and self.target_combo.count() > 0)
        self._refresh()

    # ── 刷新 ──
    def _refresh(self) -> None:
        st = self.state

        if st.mode == "auto":
            self.mode_lab.setText("AUTO")
            self.mode_lab.setObjectName("modeAuto")
            self.toggle_btn.setText("切到 MANUAL")
        else:
            self.mode_lab.setText("MANUAL")
            self.mode_lab.setObjectName("modeManual")
            self.toggle_btn.setText("切到 AUTO")
        self.mode_lab.style().unpolish(self.mode_lab)
        self.mode_lab.style().polish(self.mode_lab)

        last = st.last_route
        if last:
            self.model_lab.setText(f"{last.actual} @{last.provider}")
            w = last.warning
            self.warn_lab.setText(f"⚠ {w}" if w else "")
            self.warn_lab.setVisible(bool(w))
            lines = format_route_line(last)
            self.route_lab.setText("\n".join(lines) if lines else "（无候选记录）")
            self.route_lab.setObjectName("dim" if not lines else "")
        else:
            self.model_lab.setText("—")

        self.listen_lab.setText(
            f"监听 {self.host}:{self.port}   "
            f"局域网设备访问 http://<本机IP>:{self.port}"
        )

        # 配置摘要（有后端才知道；无后端保持空）
        if self.deps is not None:
            n_models = len({c.model for c in self.deps.candidates})
            keys = self.deps.api_keys or (
                [self.deps.api_key] if self.deps.api_key else [])
            self.cfg_lab.setText(
                f"配置 {self.config_path or '(内置)'} · "
                f"{len(self.deps.candidates)} 候选 · {n_models} 型号 · "
                f"鉴权 {len(keys)} key · "
                f"破甲{'开' if self.deps.jailbreak_enabled else '关'}"
            )

        # 账号表：每次轮询从 Pool 拉实时状态
        if self.deps is not None:
            self.state.accounts = account_rows(self.deps.candidates, self.deps.pool)
        if len(self.state.accounts) != self.table.rowCount():
            self.table.setRowCount(len(self.state.accounts))
        for i, a in enumerate(self.state.accounts):
            for col, val in enumerate((a.provider, a.account, a.model,
                                       a.status_text)):
                item = QTableWidgetItem(val)
                # 状态栏就是状态栏：全部只读（Qt 表格单元默认可双击编辑）
                item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
                self.table.setItem(i, col, item)

        self.stat_lab.setText(
            f"调用 {st.total_calls} 次 · 降级 {st.downgrade_count} 次 · "
            f"{st.total_tokens:,} tokens"
        )

    # ── 托盘模式下的关闭行为 ──
    def closeEvent(self, event) -> None:      # noqa: N802
        if self._on_quit is not None:
            # 有托盘时关窗只是隐藏
            event.ignore()
            self.hide()
        else:
            event.accept()


# ═══════════════════════════════════════════════════════════════
# 托盘
# ═══════════════════════════════════════════════════════════════
class GatewayTray(QSystemTrayIcon):
    """Windows 上的托盘常驻。

    tooltip 直接写「AUTO · 模型 · 状态」—— 不点开就知道在用谁。
    """

    def __init__(self, state: GatewayState, window: MainWindow, port: int = 8787):
        super().__init__(_make_icon())
        self.state = state
        self.window = window
        self.port = port
        self.setToolTip("AI 路由网关")
        self._build_menu()
        self.activated.connect(self._on_activated)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_tooltip)
        self._timer.start(2000)
        self._refresh_tooltip()

    def _build_menu(self) -> None:
        m = QMenu()
        act_show = QAction("显示面板", self)
        act_show.triggered.connect(self._show_window)
        m.addAction(act_show)
        act_cfg = QAction("设置…", self)
        act_cfg.triggered.connect(self.window.show_settings)
        m.addAction(act_cfg)
        m.addSeparator()

        self.act_mode = QAction("切到 MANUAL", self)
        self.act_mode.triggered.connect(self._toggle_mode)
        m.addAction(self.act_mode)
        m.addSeparator()

        act_quit = QAction("退出", self)
        act_quit.triggered.connect(self._quit)
        m.addAction(act_quit)
        self.setContextMenu(m)

    def _refresh_tooltip(self) -> None:
        last = self.state.last_route
        if last:
            tip = format_short_status(last)
            if last.model_changed:
                self.setIcon(_make_icon("warn"))
            else:
                self.setIcon(_make_icon("ok"))
        else:
            tip = f"{self.state.mode.upper()} · 等待请求"
            self.setIcon(_make_icon("ok"))
        self.setToolTip(f"AI 路由网关\n{tip}\n:{self.port}")
        self.act_mode.setText("切到 MANUAL" if self.state.mode == "auto" else "切到 AUTO")

    def _on_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.Trigger:
            if self.window.isVisible():
                self.window.hide()
            else:
                self._show_window()

    def _show_window(self) -> None:
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()

    def _toggle_mode(self) -> None:
        # 走窗口的实现 —— 那边会连 deps（事实源）一起切。
        # tray 只改 state 的话就是「假切换」：显示变了，路由没变。
        self.window._toggle_mode()
        self._refresh_tooltip()

    def _quit(self) -> None:
        self.window._on_quit = None      # 允许真正关闭
        self.window.close()
        self.hide()
        from PySide6.QtWidgets import QApplication
        QApplication.quit()
