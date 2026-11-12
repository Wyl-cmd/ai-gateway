"""GUI 资源定位 —— 源码运行和 PyInstaller 打包两种形态下找同一个图标。

## 两种运行形态

    源码：      <项目根>/assets/ai-gateway.ico
    打包 exe：  sys._MEIPASS/assets/ai-gateway.ico（PyInstaller 解包目录）

PyInstaller 单文件 exe 运行时会把 datas 解到临时目录，路径在
`sys._MEIPASS` 里 —— 写死相对路径的代码在打包后必挂（静默用回落图标，
用户看到的就是「打包版没图标」）。
"""

from __future__ import annotations

import sys
from pathlib import Path

__all__ = ["app_icon_path", "app_icon"]


def app_icon_path() -> Path | None:
    """定位图标文件；找不到返回 None（调用方回落程序化图标）。"""
    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "assets" / "ai-gateway.ico")
    # resources.py 在 src/gateway/gui/ 下，parents[3] 是项目根
    candidates.append(
        Path(__file__).resolve().parents[3] / "assets" / "ai-gateway.ico")
    for p in candidates:
        if p.is_file():
            return p
    return None


def app_icon():
    """QIcon；资源缺失时返回空 QIcon（调用方判断 isNull 再回落）。"""
    from PySide6.QtGui import QIcon

    p = app_icon_path()
    return QIcon(str(p)) if p is not None else QIcon()
