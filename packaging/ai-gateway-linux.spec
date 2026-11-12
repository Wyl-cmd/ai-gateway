# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置 —— AI 网关 Linux 单文件可执行。

与 `ai-gateway.spec`（Windows）的差异只有四处，都写在下面了。
**数据文件与 hiddenimports 两段必须与 Windows 版保持一致** ——
那两段是「漏了会静默失效」的东西，不能因为换平台就少收。

## 与 Windows 版的四处差异

① **图标格式**：Windows 用 `.ico`，Linux 桌面认 `.png` / `.svg`。
   PyInstaller 在 Linux 上忽略 `icon=`（它只对 Windows/macOS 生效），
   所以图标靠 `packaging/ai-gateway.desktop` 引用，随包分发。

② **console**：Linux 上没有「双击弹黑框」这个问题，用户要么从终端起、
   要么从 `.desktop` 起。这里设 `console=True` —— 理由：
   - 出异常时错误直接打在终端，不用去找日志文件
   - GUI 模式下多一个终端窗口是可接受代价
   想要纯 GUI 无终端，把 console 改成 False 重新构建即可（见文件末尾注释）。

③ **输出名**：加 `-linux` 后缀，避免和 Windows 产物同名，
   两者放同一个 dist/ 目录时不会互相覆盖。

④ **excludes 多排两个**：`PyQt5`/`PySide2` 在 Linux 上比 Windows 更容易被
   误扫进来（系统包管理器装的），多排一下能省 100 MB+。

构建（项目根执行）：
    .venv/bin/python -m PyInstaller packaging/ai-gateway-linux.spec \\
        --noconfirm --distpath dist --workpath build

产物：dist/ai-gateway-linux （单文件，约 30~40 MB）
"""

import os

from PyInstaller.utils.hooks import collect_data_files

# spec 内的一切相对路径都相对「本 spec 所在目录」解析 —— 用 SPECPATH
# 拼绝对路径，避免从不同工作目录构建时路径漂移
ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

# ═══════════════════════════════════════════════════════════════
# 数据文件 —— 这一段必须与 Windows 版逐字一致
# ═══════════════════════════════════════════════════════════════
#
# collect_data_files("gateway") 会自动收：
#   jailbreak/data/pliny/*.mkd   37 个破甲模板（380 KB）
#   storage/schema.sql           建表 SQL
#
# 漏了 pliny 模板的 symptom 极隐蔽：启动正常、日志无异常，
# 只是 pliny_vendor 技术读不到模板 → 破甲效果从 8208 字掉到 131 字，
# 不报任何错。漏了 schema.sql → 记忆库/产物库静默失效。
# 所以只能整体 collect，不能手工挑文件。
datas = collect_data_files("gateway")
datas += [
    (os.path.join(ROOT, "assets", "ai-gateway.png"), "assets"),
    (os.path.join(ROOT, "packaging", "ai-gateway.desktop"), "."),
]

# ═══════════════════════════════════════════════════════════════
# 隐式导入 —— 同样必须与 Windows 版逐字一致
# ═══════════════════════════════════════════════════════════════
#
# uvicorn 的协议实现在运行时按字符串动态导入（`importlib.import_module`），
# PyInstaller 的静态分析看不见，不打进来的症状是启动即崩：
#   Could not import module "uvicorn.protocols.http.h11_impl"
hiddenimports = [
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
    # 构建工具与测试不该进发行包。
    # PyQt5/PySide2 是 Linux 上额外的误扫来源（系统包管理器装的），
    # 本项目 GUI 用的是 PySide6，这两个永远用不上。
    excludes=[
        "tkinter", "pytest", "PIL", "setuptools", "pip",
        "PyQt5", "PyQt6", "PySide2",
    ],
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
    name="ai-gateway-linux",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # Linux 上 UPX 常与 glibc 冲突导致段错误，保持关闭
    upx=False,
    # console=True：异常直接打在终端，不用去找日志文件。
    # 想要纯 GUI 无终端 → 改成 False 重新构建（见下方说明）
    console=True,
    # icon= 在 Linux 上被 PyInstaller 忽略（只对 Windows/macOS 生效），
    # 图标由随包分发的 ai-gateway.desktop 引用 assets/ai-gateway.png
    icon=None,
    version=None,
)

# ── 想要纯 GUI 无终端窗口的版本 ──────────────────────────────
# 把上面 console=True 改成 False 重新构建。
# 代价：GUI 启动失败时错误不可见，只能靠 exe_entry.py 写的错误日志：
#   ~/.local/share/ai-gateway/ai-gateway-error.log
