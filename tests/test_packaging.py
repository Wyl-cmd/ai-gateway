"""打包相关回归测试 —— 守住两个「只在打包版才翻车」的坑。

① windowed（无控制台）exe 里 sys.stdout/stderr 是 None：
   uvicorn 日志格式化器调 sys.stdout.isatty() → 启动即崩
   （实测：双击 exe 弹「Unable to configure formatter 'default'」）

② schema.sql 必须打进包：storage/repo.py 靠它建表，漏了就是
   「记忆/产物库静默失效」——启动正常、破甲排序退化，不报错
   （实测：exe 启动日志出现 schema.sql 缺失警告）
"""

from __future__ import annotations

import importlib.util
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_exe_entry():
    """packaging/exe_entry.py 不在包里，按路径加载。"""
    spec = importlib.util.spec_from_file_location(
        "exe_entry_for_test", ROOT / "packaging" / "exe_entry.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── ① windowed 模式的标准流守卫 ──
def test_无控制台时_标准流被兜底(monkeypatch):
    mod = _load_exe_entry()
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    mod._ensure_std_streams()
    try:
        assert sys.stdout is not None and sys.stderr is not None
        sys.stdout.write("x")          # 能写不炸
        sys.stderr.write("y")
        assert sys.stdout.isatty() is False   # uvicorn 会调这个
    finally:
        monkeypatch.undo()


def test_有控制台时_标准流不动(monkeypatch):
    mod = _load_exe_entry()
    sentinel = sys.stdout
    mod._ensure_std_streams()
    assert sys.stdout is sentinel


# ── ② schema.sql 必须声明进包数据 ──
def test_schema_sql_已声明进包数据():
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pd = cfg["tool"]["setuptools"]["package-data"]
    assert "schema.sql" in pd.get("gateway.storage", []), \
        "storage/schema.sql 没声明 —— 装出来的包记忆/产物库会静默失效"
    assert pd.get("gateway.jailbreak"), "破甲模板声明不能丢"


def test_pyinstaller_spec_收集全部包数据():
    """spec 不能只挑 .mkd —— 那样 schema.sql 会漏（实测漏过）。"""
    spec = (ROOT / "packaging" / "ai-gateway.spec").read_text(encoding="utf-8")
    assert 'collect_data_files("gateway")' in spec
    assert "excludes" in spec
