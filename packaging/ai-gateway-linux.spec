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
    excludes=[
        # ── 开发/构建期依赖 ──
        "tkinter", "pytest", "PIL", "setuptools", "pip",
        "PyQt5", "PyQt6", "PySide2",
        # ── PySide6 里用不到的子系统（实测砍掉 136MB → 约 55MB）──
        #
        # 本项目 GUI 只用到 QtCore / QtGui / QtWidgets（+ QtSvg 画图标）。
        # PyInstaller 的 PySide6 hook 默认会整包收，而 PySide6 完整发行版
        # 含 QtWebEngine（自带一个 Chromium，单它就有 100MB+）、Qt3D、
        # QtQuick、QtMultimedia 等，全是不用的。
        #
        # 排除原则：**只排「绝不可能是 QtWidgets 依赖」的顶层模块**。
        # 有争议的一律保留 —— 排错一个的表现是「双击无反应」或
        # 「Could not find the Qt platform plugin」，比包大难查得多。
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtWebEngineQuick",
        "PySide6.QtWebChannel",
        "PySide6.QtWebSockets",
        "PySide6.Qt3DCore",
        "PySide6.Qt3DRender",
        "PySide6.Qt3DInput",
        "PySide6.Qt3DLogic",
        "PySide6.Qt3DAnimation",
        "PySide6.Qt3DExtras",
        "PySide6.QtCharts",
        "PySide6.QtDataVisualization",
        "PySide6.QtGraphs",
        "PySide6.QtMultimedia",
        "PySide6.QtMultimediaWidgets",
        "PySide6.QtQuick",
        "PySide6.QtQuick3D",
        "PySide6.QtQuickWidgets",
        "PySide6.QtQuickControls2",
        "PySide6.QtQml",
        "PySide6.QtBluetooth",
        "PySide6.QtNfc",
        "PySide6.QtPositioning",
        "PySide6.QtLocation",
        "PySide6.QtSensors",
        "PySide6.QtSerialPort",
        "PySide6.QtSerialBus",
        "PySide6.QtRemoteObjects",
        "PySide6.QtScxml",
        "PySide6.QtStateMachine",
        "PySide6.QtTest",
        "PySide6.QtHelp",
        "PySide6.QtUiTools",
        "PySide6.QtDesigner",
        "PySide6.QtOpenGL",
        "PySide6.QtOpenGLWidgets",
        "PySide6.QtPdf",
        "PySide6.QtPdfWidgets",
        "PySide6.QtSpatialAudio",
        "PySide6.QtTextToSpeech",
        "PySide6.QtSql",
        # ── 注意：以下**故意不可排除** ──
        # PySide6.QtNetwork   —— QtWidgets/QtGui 内部会用到（QSslSocket 等）
        # PySide6.QtSvg       —— resources.py 可能用 SVG 图标
        # PySide6.QtPrintSupport —— QTextEdit/QMessageBox 的打印路径
        # PySide6.QtDBus      —— Linux 桌面集成（托盘、通知）需要
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

# ═══════════════════════════════════════════════════════════════
# 过滤 Qt 的二进制与资源 —— 这一步才是真正减重的地方
# ═══════════════════════════════════════════════════════════════
#
# 为什么上面的 excludes 不管用：
#   `excludes` 只作用于 Python 模块的导入分析。PyInstaller 的 PySide6 hook
#   是**直接遍历 Qt 安装目录**把 .so / .dll / plugin 收进 a.binaries 和
#   a.datas 的，那些是二进制文件不是 Python 模块，excludes 拦不住。
#   实测：加了 40 个 PySide6 excludes，包体 136.2 MB 一点没变。
#
# 真正有效的是在 Analysis 之后过滤这两个列表。
#
# 排除原则：只排「绝不可能被 QtWidgets 依赖」的顶层库。
# 排错一个的症状是「双击无反应」或「Could not find the Qt platform plugin
# (xcb)」，比包大难查十倍 —— 所以清单保持保守，有疑问的一律留下。
_QT_DROP = (
    "Qt6WebEngineCore", "Qt6WebEngineWidgets", "Qt6WebEngineQuick",
    "Qt6WebChannel", "Qt6WebSockets", "Qt6Pdf", "Qt6PdfQuick",
    "Qt6Quick", "Qt6Quick3D", "Qt6QuickControls2", "Qt6QuickWidgets",
    "Qt6QuickTemplates2", "Qt6QuickLayouts", "Qt6QuickDialogs2",
    "Qt6Qml", "Qt6QmlModels", "Qt6QmlWorkerScript", "Qt6QmlCompiler",
    "Qt6QmlLocalStorage", "Qt6QmlXmlListModel",
    "Qt63DCore", "Qt63DRender", "Qt63DInput", "Qt63DLogic",
    "Qt63DAnimation", "Qt63DExtras", "Qt63DQuick",
    "Qt6Charts", "Qt6ChartsQml", "Qt6DataVisualization",
    "Qt6Graphs", "Qt6Multimedia", "Qt6MultimediaQuick",
    "Qt6MultimediaWidgets", "Qt6SpatialAudio",
    "Qt6TextToSpeech", "Qt6Bluetooth", "Qt6Nfc",
    "Qt6Positioning", "Qt6PositioningQuick", "Qt6Location",
    "Qt6Sensors", "Qt6SensorsQuick", "Qt6SerialPort", "Qt6SerialBus",
    "Qt6RemoteObjects", "Qt6Scxml", "Qt6StateMachine", "Qt6StateMachineQml",
    "Qt6Test", "Qt6Help", "Qt6UiTools", "Qt6Designer",
    "Qt6Sql", "Qt6Concurrent",
    # 资源类：翻译/文档/示例都不需要
    "qtwebengine_resources", "qtwebengine_devtools_resources",
    "qtwebengine_dictionaries", "translations/qtwebengine",
)


def _keep(name: str) -> bool:
    """False = 丢掉这个条目。name 是包内路径，如 PySide6/Qt6/lib/libQt6Quick.so.6"""
    return not any(k in name for k in _QT_DROP)


_before_b = len(a.binaries)
_before_d = len(a.datas)
_before_sz = sum(os.path.getsize(f) for f, _, _ in a.binaries if os.path.exists(f))
a.binaries = [b for b in a.binaries if _keep(b[0])]
a.datas = [d for d in a.datas if _keep(d[0])]
_after_sz = sum(os.path.getsize(f) for f, _, _ in a.binaries if os.path.exists(f))
print(f"[spec] binaries {_before_b} → {len(a.binaries)} 项，"
      f"{( _before_sz - _after_sz)/1024/1024:.1f} MB 已排除")
print(f"[spec] datas    {_before_d} → {len(a.datas)} 项")

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ai-gateway-linux",
    debug=False,
    bootloader_ignore_signals=False,
    # strip=True —— Linux 包体从 128 MB 降到约 40 MB 的关键
    #
    # 实测：Hermes 工具链自带的 Python 3.14.7 是未 strip 构建，
    # libpython3.14.so.1.0 单它有 236.9 MB（系统 /usr/lib 那个只有 7.9 MB）。
    # strip=False 时 PyInstaller 原样打进去，成品 136 MB 里大部分是调试符号。
    # 改成 True 让 PyInstaller 对所有二进制执行 strip。
    #
    # Windows 版保持 strip=False —— 那边没有 strip 工具链，
    # 开了 PyInstaller 会调不到 strip 而报错。
    strip=True,
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
