"""GUI 入口 —— 读 config.toml 装配后端，托盘/窗口 + 进程内服务。

## 和之前版本的区别（接线完成）

之前这里只起一个空面板：那句「监听 0.0.0.0:8787」是写死的文案，
进程内并没有服务。现在真实装配：

    config.toml ──load_config──▶ assemble ──▶ GatewayDeps
                                                   │
                              create_app + uvicorn（qasync 同一事件循环）
                                                   │
                     MainWindow(deps) / GatewayTray —— 真实账号表、路由链、统计

## 为什么用 qasync 而不是后端开线程

「后端和 GUI 是同一个进程」的既定设计。qasync 把 asyncio 事件循环挂到
Qt 上：uvicorn 的协程和 Qt 信号在同一循环里跑，**同一线程** ——
on_request_done 回调可以直接更新 GUI 状态，不需要跨线程封送。
Windows 的 ProactorEventLoop 兼容性也靠它（qasync 是 [gui] extra 的原因）。

## 双平台行为

**Windows**：托盘常驻。关窗 = 隐藏到托盘，网关继续跑。
单击托盘图标显隐面板，右键菜单能切 auto/manual 和退出。

**Linux**：普通窗口。关窗 = 退出（GNOME 默认没托盘，藏起来就找不回来了）。

## 托盘不可用时自动降级

Windows Server Core / 远程会话 / 精简版可能没有系统托盘，
`QSystemTrayIcon.isSystemTrayAvailable()` 返回 False —— 这时退窗口，
不能崩，也不能「启动后图标不出现且无窗口」（那就成幽灵进程了）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys


def run_gui(*, host: str | None = None, port: int | None = None,
            config_path: str = "config.toml",
            state=None) -> int:
    """启动 GUI（含进程内网关服务）。返回退出码。

    host/port 为 None 时用 config.toml 里的 [server] 值。
    """
    try:
        import qasync
    except ImportError:
        print("缺 qasync。装：pip install -e '.[gui]'", file=sys.stderr)
        return 1
    try:
        from PySide6.QtWidgets import QApplication, QSystemTrayIcon
    except ImportError:
        print("缺 PySide6。装：pip install -e '.[gui]'", file=sys.stderr)
        return 1

    from ..app import create_app
    from ..config import load_config
    from ..runtime import assemble
    from .state import GatewayState

    # ── 装配（后端是真的）──
    config = load_config(config_path)
    deps = assemble(config)
    host = host or config.server.host
    port = port or config.server.port

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("AI 路由网关")
    app.setQuitOnLastWindowClosed(False)      # 关窗不退出（托盘模式下要常驻）

    st = state or GatewayState()
    st.mode = deps.mode                       # 显示层跟配置对齐

    # ── 服务：挂在 Qt 的 asyncio 循环上（同线程）──
    import uvicorn

    def _new_server(d) -> "uvicorn.Server":
        return uvicorn.Server(uvicorn.Config(
            create_app(d), host=host, port=port, log_level="warning"))

    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    server = _new_server(deps)
    server_task = loop.create_task(server.serve())

    def _stop_server() -> None:
        server.should_exit = True             # 让 uvicorn 优雅退出，不留端口占用

    app.aboutToQuit.connect(_stop_server)

    # ── 设置保存的编排：密钥确认 → 写文件 → 热重载 ──
    def _on_config_saved(new_cfg, secrets) -> None:
        from PySide6.QtWidgets import QMessageBox

        from .config_editor import serialize_config, set_user_env_var

        # ① 密钥写环境变量（要用户确认 —— 这是改系统状态的动作）
        if secrets:
            names = ", ".join(k for k, _ in secrets)
            if QMessageBox.question(
                    win, "写入密钥",
                    f"将把 {len(secrets)} 个密钥写入系统环境变量（{names}），\n"
                    f"密钥不会写进配置文件。继续？") == QMessageBox.Yes:
                try:
                    for k, v in secrets:
                        set_user_env_var(k, v)
                except Exception as exc:              # noqa: BLE001
                    QMessageBox.warning(win, "密钥写入失败", str(exc))
                    return
        # ② 配置写回文件（先写临时文件再替换，写一半断电不会毁掉原配置）
        import os
        import tempfile
        from pathlib import Path
        try:
            text = serialize_config(new_cfg)
            p = Path(config_path)
            fd, tmp = tempfile.mkstemp(dir=str(p.parent or "."), suffix=".toml")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, p)
        except Exception as exc:                      # noqa: BLE001
            QMessageBox.warning(win, "配置写入失败", str(exc))
            return
        # ③ 热重载：停旧服务 → 重新装配 → 起新服务 → 面板换新 deps
        async def _restart() -> None:
            nonlocal server, server_task
            server.should_exit = True
            try:
                await server_task
            except Exception:                         # noqa: BLE001
                pass
            await asyncio.sleep(0.3)          # 等 OS 释放端口（Windows 需要）
            new_deps = assemble(new_cfg)
            server = _new_server(new_deps)
            server_task = loop.create_task(server.serve())
            win.set_deps(new_deps, new_cfg)
            if mode == GUIMode.TRAY:
                tray.showMessage("AI 路由网关", "配置已保存并重载",
                                 QSystemTrayIcon.Information, 2000)

        loop.create_task(_restart())

    # 运行时才探测托盘能力 —— 静态检测不到
    from .platform import GUIMode, gui_mode
    tray_available = QSystemTrayIcon.isSystemTrayAvailable()
    mode = gui_mode(tray_available=tray_available)

    from .main_window import MainWindow

    if mode == GUIMode.TRAY:
        from .main_window import GatewayTray

        win = MainWindow(st, host=host, port=port, on_quit="tray",
                         deps=deps, config=config, config_path=config_path,
                         on_config_saved=_on_config_saved)
        tray = GatewayTray(st, win, port=port)
        tray.show()
        win.hide()                            # 托盘模式启动时不弹窗
        tray.showMessage(
            "AI 路由网关",
            f"已在后台运行，监听 {host}:{port}\n点击托盘图标打开面板",
            QSystemTrayIcon.Information,
            3000,
        )
        with loop:
            loop.run_forever()
        return 0

    # 窗口模式
    win = MainWindow(st, host=host, port=port, on_quit=None,
                     deps=deps, config=config, config_path=config_path,
                     on_config_saved=_on_config_saved)
    win.show()
    with loop:
        loop.run_forever()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="gateway-gui",
                                description="AI 路由网关图形界面（含网关服务）")
    p.add_argument("--config", default="config.toml",
                   help="配置文件路径（默认 ./config.toml）")
    p.add_argument("--host", default=None,
                   help="监听地址（默认取配置 [server].host）")
    p.add_argument("--port", type=int, default=None,
                   help="监听端口（默认取配置 [server].port）")
    a = p.parse_args()
    return run_gui(host=a.host, port=a.port, config_path=a.config)


if __name__ == "__main__":
    raise SystemExit(main())
