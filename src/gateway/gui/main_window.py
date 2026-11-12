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

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QFont, QIcon, QPainter, QPixmap, QColor
from PySide6.QtWidgets import (
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

from .state import GatewayState, format_route_line, format_short_status


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
                 port: int = 8787, on_quit=None) -> None:
        super().__init__()
        self.state = state
        self.host = host
        self.port = port
        self._on_quit = on_quit

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
        self.toggle_btn = QPushButton("切到 MANUAL")
        self.toggle_btn.clicked.connect(self._toggle_mode)
        btn_row.addWidget(self.toggle_btn)
        outer.addLayout(btn_row)

        self.setCentralWidget(root)
        self._refresh()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(1000)

    # ── 交互 ──
    def _toggle_mode(self) -> None:
        self.state.set_mode("manual" if self.state.mode == "auto" else "auto")
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

        # 账号表
        if len(self.state.accounts) != self.table.rowCount():
            self.table.setRowCount(len(self.state.accounts))
        for i, a in enumerate(self.state.accounts):
            self.table.setItem(i, 0, QTableWidgetItem(a.provider))
            self.table.setItem(i, 1, QTableWidgetItem(a.account))
            self.table.setItem(i, 2, QTableWidgetItem(a.model))
            self.table.setItem(i, 3, QTableWidgetItem(a.status_text))

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
        self.state.set_mode("manual" if self.state.mode == "auto" else "auto")
        self._refresh_tooltip()

    def _quit(self) -> None:
        self.window._on_quit = None      # 允许真正关闭
        self.window.close()
        self.hide()
        from PySide6.QtWidgets import QApplication
        QApplication.quit()
