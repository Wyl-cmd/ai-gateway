"""GUI 平台检测 —— 决定用托盘还是普通窗口。

## 为什么 Windows 用托盘、Linux 用窗口

**Windows 托盘**：关窗后还在后台跑（网关要常驻），点托盘图标能看状态。
用户不需要为了看一眼「现在走的哪个模型」去开一个窗口。

**Linux 不用托盘**：Linux 托盘依赖桌面环境 —— GNOME 默认不给、
Plasma 可以、XFCE 要装插件。塞进去的结果是「有的机器有、有的没有」，
而且托盘区在 Linux 上通常已经很拥挤。普通窗口最可靠。

**双平台降级**：Windows 上也可能没有系统托盘（Server Core、远程会话、
某些精简版）。检测不到就退窗口，不能崩。
"""

from __future__ import annotations

import sys
from enum import StrEnum


class GUIMode(StrEnum):
    TRAY = "tray"        # 托盘常驻（Windows）
    WINDOW = "window"    # 普通窗口（Linux / macOS / 兜底）


def gui_mode(platform: str | None = None, *, tray_available: bool = True) -> GUIMode:
    """决定用哪种形态。

    `platform` 可注入，方便测试；不传就取 `sys.platform`。
    `tray_available` 由调用方在运行时探测（`QSystemTrayIcon.isSystemTrayAvailable()`），
    静态检测不到托盘能力 —— 那要等 Qt 起来才知道。
    """
    plat = (platform if platform is not None else sys.platform).lower()
    if plat.startswith("win") and tray_available:
        return GUIMode.TRAY
    return GUIMode.WINDOW
