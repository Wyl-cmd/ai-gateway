# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置 —— AI 网关 Windows 单文件 exe。

## 三个关键点（每个都有对应的翻车方式）

① **破甲模板必须打包**：`gateway/jailbreak/data/pliny/*.mkd`（37 个）。
   不进包的 symptom 很隐蔽——启动正常，只是破甲效果突然变差，不报错。

② **uvicorn 的协议实现是字符串动态导入**，PyInstaller 静态分析看不见，
   必须显式 hiddenimports（否则启动时报 `Could not import module
   "uvicorn.protocols.http.h11_impl"`）。

③ **windowed（console=False）**：双击不弹黑框。代价是异常不可见 ——
   由 packaging/exe_entry.py 负责写错误日志 + 弹窗。

构建（项目根执行）：
    .venv\\Scripts\\python -m PyInstaller packaging\\ai-gateway.spec ^
        --noconfirm --distpath dist --workpath build
"""

import os

from PyInstaller.utils.hooks import collect_data_files

# spec 内的一切相对路径都相对「本 spec 所在目录」解析 —— 用 SPECPATH
# 拼绝对路径，避免从不同工作目录构建时路径漂移
ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

# 破甲模板 + schema.sql + 应用图标。
# 注意不能只挑 .mkd：storage/schema.sql 漏了 = 记忆/产物库静默失效（实测漏过）
datas = collect_data_files("gateway")
datas += [(os.path.join(ROOT, "assets", "ai-gateway.ico"), "assets")]

hiddenimports = [
    # uvicorn：字符串动态导入，静态分析不可见
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
]

a = Analysis(
    [os.path.join(SPECPATH, "exe_entry.py")],
    pathex=[os.path.join(ROOT, "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # 构建工具与测试不该进发行包
    excludes=["tkinter", "pytest", "PIL", "setuptools", "pip"],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ai-gateway",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                     # windowed：双击无黑框
    icon=os.path.join(ROOT, "assets", "ai-gateway.ico"),
    version=None,
)
