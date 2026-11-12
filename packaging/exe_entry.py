"""PyInstaller 打包入口 —— 双击即显示面板的托盘版网关。

## 为什么要有这个文件（而不是直接打 gateway.gui.app）

  ① 路径基准：双击 exe 时工作目录不一定是 exe 所在目录，配置文件必须
     以 **exe 所在目录** 为准（config.toml / secrets.toml 都在那里）
  ② 启动失败可见：windowed（无控制台）exe 里异常是静默的 —— 必须写
     错误日志 + 弹窗，否则用户看到的是「双击没反应」
  ③ multiprocessing.freeze_support()：PyInstaller 单文件 exe 的固定要求
"""

from __future__ import annotations

import multiprocessing
import os
import sys
import traceback
from pathlib import Path


def _exe_dir() -> Path:
    """exe 所在目录；源码直接跑时退回当前目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def _ensure_std_streams() -> None:
    """windowed exe（无控制台）里 sys.stdout/stderr 是 None —— 兜底成日志文件。

    不兜底的后果（实测踩到）：uvicorn 的日志格式化器构造时调
    `sys.stdout.isatty()` → AttributeError →「Unable to configure
    formatter 'default'」→ 双击 exe 直接崩。启动函数里的任何 print
    （如装配警告）也会跟着炸。

    写进 exe 同目录的 ai-gateway.log（追加）：既避免崩溃，又留下诊断线索。
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    log = _exe_dir() / "ai-gateway.log"
    try:
        stream = open(log, "a", encoding="utf-8", buffering=1)
    except Exception:                       # noqa: BLE001
        stream = open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


def _report_crash(exc: BaseException) -> Path:
    """写错误日志 + 弹窗（弹窗失败也要保住日志）。"""
    log = _exe_dir() / "ai-gateway-error.log"
    try:
        log.write_text(
            "AI 网关启动失败\n\n"
            + "".join(traceback.format_exception(exc)),
            encoding="utf-8")
    except Exception:                     # noqa: BLE001
        pass
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
        app = QApplication.instance() or QApplication([])
        QMessageBox.critical(
            None, "AI 网关启动失败",
            f"{type(exc).__name__}: {exc}\n\n详情已写入：\n{log}")
    except Exception:                     # noqa: BLE001
        pass
    return log


def _diag() -> int:
    """打包自检：把关键资源状态写进 ai-gateway-diag.log。

    windowed exe 没有控制台，出问题时只能靠这类日志取证。
    用法：ai-gateway.exe --diag
    """
    lines = [f"frozen={getattr(sys, 'frozen', False)}",
             f"meipass={getattr(sys, '_MEIPASS', None)}",
             f"exe_dir={_exe_dir()}"]
    try:
        import gateway
        lines.append(f"gateway={gateway.__file__}")
        import gateway.jailbreak.pliny as p
        lines.append(f"pliny_module={p.__file__}")
        d = p._template_dir()
        lines.append(f"template_dir={d}")
        if d is not None:
            lines.append(f"mkd_on_disk={len(list(d.glob('*.mkd')))}")
        lines.append(f"templates_loaded={len(p.load_all())}")
        lines.append(f"deepseek_templates={len(p.templates_for('deepseek-chat'))}")
        import gateway.storage.repo as r
        lines.append(f"schema_exists={Path(r.__file__).with_name('schema.sql').is_file()}")
    except Exception:                     # noqa: BLE001
        lines.append(traceback.format_exc())
    out = _exe_dir() / "ai-gateway-diag.log"
    out.write_text("\n".join(lines), encoding="utf-8")
    return 0


def main() -> int:
    multiprocessing.freeze_support()
    _ensure_std_streams()                 # 必须在任何可能 print 的代码之前
    if "--diag" in sys.argv:
        return _diag()
    try:
        from gateway.gui.app import run_gui

        # 配置以 exe 所在目录为准（双击启动时 CWD 可能是别处）
        cfg = _exe_dir() / "config.toml"
        return run_gui(config_path=str(cfg), show=True)
    except BaseException as exc:          # noqa: BLE001
        _report_crash(exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
