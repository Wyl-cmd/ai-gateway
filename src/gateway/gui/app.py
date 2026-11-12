"""GUI 入口 —— 平台检测后启动托盘或窗口。

## 双平台行为

**Windows**：托盘常驻。关窗 = 隐藏到托盘，网关继续跑。
单击托盘图标显隐面板，右键菜单能切 auto/manual 和退出。

**Linux**：普通窗口。关窗 = 退出（Linux 用户习惯这样，
而且 GNOME 默认没托盘，藏起来就找不回来了）。

## 托盘不可用时自动降级

Windows Server Core / 远程会话 / 精简版可能没有系统托盘，
`QSystemTrayIcon.isSystemTrayAvailable()` 返回 False —— 这时退窗口，
不能崩，也不能「启动后图标不出现且无窗口」（那就成幽灵进程了）。

## 后端和 GUI 是同一个进程

不搞前后端分离的子进程 —— 自用工具，多一层进程就多一层故障面
（孤儿进程、端口占用、日志分散）。GUI 直接持有 GatewayDeps，
将来的「实时状态」靠回调或 QTimer 轮询即可。
"""

from __future__ import annotations

import sys

from .platform import GUIMode, gui_mode
from .state import GatewayState


def run_gui(*, host: str = "0.0.0.0", port: int = 8787,
            state: GatewayState | None = None) -> int:
    """启动 GUI。返回退出码。"""
    try:
        from PySide6.QtWidgets import QApplication, QSystemTrayIcon
    except ImportError:
        print("缺 PySide6。装：pip install -e '.[gui]'", file=sys.stderr)
        return 1

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("AI 路由网关")
    app.setQuitOnLastWindowClosed(False)      # 关窗不退出（托盘模式下要常驻）

    st = state or GatewayState()

    # 运行时才探测托盘能力 —— 静态检测不到
    tray_available = QSystemTrayIcon.isSystemTrayAvailable()
    mode = gui_mode(tray_available=tray_available)

    from .main_window import MainWindow

    if mode == GUIMode.TRAY:
        from .main_window import GatewayTray

        win = MainWindow(st, host=host, port=port, on_quit="tray")
        tray = GatewayTray(st, win, port=port)
        tray.show()
        win.hide()                            # 托盘模式启动时不弹窗
        tray.showMessage(
            "AI 路由网关",
            f"已在后台运行，监听 {host}:{port}\n点击托盘图标打开面板",
            QSystemTrayIcon.Information,
            3000,
        )
        return app.exec()

    # 窗口模式
    win = MainWindow(st, host=host, port=port, on_quit=None)
    win.show()
    return app.exec()


def main() -> int:
    import argparse

    p = argparse.ArgumentParser(prog="gateway-gui", description="AI 路由网关图形界面")
    p.add_argument("--host", default="0.0.0.0",
                   help="监听地址（默认 0.0.0.0 = 局域网可访问）")
    p.add_argument("--port", type=int, default=8787)
    a = p.parse_args()
    return run_gui(host=a.host, port=a.port)


if __name__ == "__main__":
    raise SystemExit(main())
