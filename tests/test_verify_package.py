"""打包产物验证器的测试（`packaging/verify_package.py`）。

## 被测缺陷（2026-11-15）

`build_linux.sh --verify` 用 `strings | grep` 判断数据文件在不在包里，导致每次都
误报「✗ 自检未通过」。两个独立成因：

1. `zw-enc2-v1` 在本项目里根本不存在（是另一个项目的串）
2. onefile 的 datas 压缩存放，压缩字节流里没有明文

## 本测试守什么

- **不能再用 `strings` 判内容**（结构判据）
- 三态分明：`pass` / `fail` / `unverified`，**「没验证」绝不等于「通过」**
- 「没读到」不等于「是 0」（探针失败不能被当成模板缺失）
- 判据要真的能抓出「模板没打进去」这种事故（不是只会在好东西上返回 pass）
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MOD_PATH = ROOT / "packaging" / "verify_package.py"

#: 真产物路径（`skipif` 在**模块导入时**就求值，所以必须定义在第一个使用它们的装饰器之前）
LINUX_BIN = ROOT / "dist" / "ai-gateway-linux"
WIN_EXE = ROOT / "dist-win" / "ai-gateway.exe"


def _load():
    """按路径加载（packaging/ 不是包，不能 import）。

    **必须注册进 `sys.modules`**：`verify_package.py` 用了
    `from __future__ import annotations`，`@dataclass` 解析字符串注解时要按
    `cls.__module__` 反查模块。不注册就查不到 → `AttributeError`（实测踩到）。
    """
    import sys

    spec = importlib.util.spec_from_file_location("verify_package_for_test", MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ══════════════════════════════════════════════════════════════════
# 一、日志解析：读不到 ≠ 是 0
# ══════════════════════════════════════════════════════════════════

def test_解析正常日志() -> None:
    vp = _load()
    text = (
        "frozen=True\n"
        "meipass=/tmp/_MEI123\n"
        "exe_dir=/work/dist\n"
        "mkd_on_disk=37\n"
        "templates_loaded=39\n"
        "deepseek_templates=6\n"
        "schema_exists=True\n"
    )
    f = vp.parse_diag_log(text)
    assert f.frozen is True
    assert f.mkd_on_disk == 37
    assert f.templates_loaded == 39
    assert f.schema_exists is True


def test_缺行时留None而不是0() -> None:
    """探针只写出部分行时，没读到的项必须是 `None`。

    若把「没读到」当成 0，就会把「探针失败」误判成「模板缺失」——
    那是对着坏证据下结论，比不判还坏。
    """
    vp = _load()
    f = vp.parse_diag_log("frozen=True\n")
    assert f.mkd_on_disk is None
    assert f.templates_loaded is None
    assert f.schema_exists is None
    assert vp.judge_diag(f) != []          # 不能因为「全是 None」而判通过


def test_崩溃日志不算通过() -> None:
    """`--diag` 内部异常时日志里是 traceback，不能解析成通过。"""
    vp = _load()
    text = "frozen=True\ntraceback...\nModuleNotFoundError: No module named 'gateway'\n"
    f = vp.parse_diag_log(text)
    assert vp.judge_diag(f) != []


# ══════════════════════════════════════════════════════════════════
# 二、判据：好东西要通过，坏东西必须红
# ══════════════════════════════════════════════════════════════════

def test_模板齐_判通过() -> None:
    vp = _load()
    f = vp.DiagFacts(frozen=True, mkd_on_disk=37, templates_loaded=39, schema_exists=True)
    assert vp.judge_diag(f) == []


def test_模板缺失_判失败() -> None:
    """这就是自检本来要抓的事故：模板没进包 → 破甲静默失效（8208 字掉到 131 字）。"""
    vp = _load()
    f = vp.DiagFacts(frozen=True, mkd_on_disk=0, templates_loaded=0, schema_exists=True)
    fails = vp.judge_diag(f)
    assert fails, "模板 0 个竟然判通过 —— 这道门等于没有"


def test_模板数量不对_判失败() -> None:
    """数量少于预期说明打包漏了文件（比如 collect 只收到一部分）。"""
    vp = _load()
    f = vp.DiagFacts(frozen=True, mkd_on_disk=12, templates_loaded=12, schema_exists=True)
    assert vp.judge_diag(f) != []


def test_schema缺失_判失败() -> None:
    vp = _load()
    f = vp.DiagFacts(frozen=True, mkd_on_disk=37, templates_loaded=39, schema_exists=False)
    assert vp.judge_diag(f) != []


# ══════════════════════════════════════════════════════════════════
# 三、三态：unverified 不能被当成 pass
# ══════════════════════════════════════════════════════════════════

def test_跑不了也读不了_返回unverified(tmp_path: Path) -> None:
    """给一个不存在的路径：既不能跑、也读不出包内清单。

    正确结论是 `unverified`（未验证），**绝不能是 `pass`**。
    """
    vp = _load()
    v = vp.verify(tmp_path / "不存在的东西")
    assert v.status == "unverified", f"未验证却报 {v.status} —— 『没验证』不等于『通过』"
    assert not v.ok


def test_verdict的ok只认pass() -> None:
    vp = _load()
    assert vp.Verdict("pass", "x").ok is True
    assert vp.Verdict("fail", "x").ok is False
    assert vp.Verdict("unverified", "x").ok is False


def test_三态映射三个不同退出码() -> None:
    """`pass`/`fail`/`unverified` 必须对应 0/1/2 三个**不同**退出码。

    压成一两个的话，脚本层面就分不出「没验证」和「通过」还是「失败」——
    而 `build_linux.sh --verify` 正是靠这个决定打 ✓ / ✗ / !。
    """
    vp = _load()
    codes = vp.EXIT_CODES
    assert codes["pass"] == 0
    assert set(codes) == {"pass", "fail", "unverified"}
    assert len(set(codes.values())) == 3, f"三态没映射到三个不同码: {codes}"


def test_命令行对不存在的产物返回2(tmp_path: Path) -> None:
    """行为级：产物不存在 → 退出码 2（未验证），不是 1 也不是 0。"""
    vp = _load()
    assert vp.main([str(tmp_path / "没有这个文件")]) == 2


@pytest.mark.skipif(not LINUX_BIN.is_file(), reason="未构建 Linux 产物")
def test_命令行对真产物返回0() -> None:
    """行为级：真产物 → 0。"""
    vp = _load()
    assert vp.main([str(LINUX_BIN)]) == 0


@pytest.mark.skipif(not WIN_EXE.is_file(), reason="未下载 Windows exe")
def test_命令行对Windows产物返回0_且走archive() -> None:
    """行为级（**双端**）：Linux 上验 Windows exe → 0，且手段降级为 archive。"""
    vp = _load()
    assert vp.main([str(WIN_EXE)]) == 0
    assert vp.verify(WIN_EXE).method == "archive"


# ══════════════════════════════════════════════════════════════════
# 四、真产物集成（有产物才跑；路径在文件顶部定义）
# ══════════════════════════════════════════════════════════════════


@pytest.mark.skipif(not LINUX_BIN.is_file(), reason="未构建 Linux 产物")
def test_真Linux产物_运行时探针判通过() -> None:
    """对真产物跑 `--diag`，必须通过且手段是 runtime-diag。"""
    vp = _load()
    v = vp.verify(LINUX_BIN)
    assert v.status == "pass", f"{v.status}: {v.failures}"
    assert v.method == "runtime-diag", f"应优先用运行时探针，实际 {v.method}"


@pytest.mark.skipif(not LINUX_BIN.is_file(), reason="未构建 Linux 产物")
def test_相对路径也能走运行时探针(monkeypatch) -> None:
    """传**相对路径**时也必须能走运行时探针。

    ⚠️ 实测踩到：`run_diag` 里用 `cwd=binary.parent` 启动子进程，若 `binary` 是
    相对路径（`dist/ai-gateway-linux`），实际执行的是 `dist/dist/ai-gateway-linux`
    → 不存在 → 静默降级到 archive。**降级本身没错，但这次降级是假的**：
    产物明明能跑，只是路径拼错了。构建脚本正是用相对路径调用验证器的，
    所以这个 bug 会让每次 `--verify` 都白丢最强的那一级判据。
    """
    vp = _load()
    monkeypatch.chdir(ROOT)
    v = vp.verify(Path("dist") / "ai-gateway-linux")
    assert v.method == "runtime-diag", (
        f"相对路径没能走运行时探针，降级成了 {v.method} —— 路径没解析成绝对路径"
    )
    assert v.status == "pass", v.failures


@pytest.mark.skipif(not LINUX_BIN.is_file(), reason="未构建 Linux 产物")
def test_真Linux产物_包内确实有37个mkd() -> None:
    """绕过运行时，直接读包内清单 —— 这条同时证明「压缩」这个成因。"""
    vp = _load()
    info = vp.read_archive(LINUX_BIN)
    assert info["error"] is None, info["error"]
    assert info["mkd"] == 37, f"包内 .mkd 应为 37，实际 {info['mkd']}"
    assert info["schema_sql"] is True


@pytest.mark.skipif(not WIN_EXE.is_file(), reason="未下载 Windows exe")
def test_真Windows_exe_也能读包内清单() -> None:
    """**双端支持的核心判据**：PE 格式用同一套代码也能读出清单。

    Windows exe 在 Linux 上跑不起来（运行时探针会降级），但 CArchive 解析是
    纯 Python、与平台无关 —— 所以这里能证明 Windows 包内容确实完整。
    """
    vp = _load()
    info = vp.read_archive(WIN_EXE)
    assert info["error"] is None, info["error"]
    assert info["mkd"] == 37, f"Windows 包内 .mkd 应为 37，实际 {info['mkd']}"
    assert info["schema_sql"] is True


@pytest.mark.skipif(not WIN_EXE.is_file(), reason="未下载 Windows exe")
def test_真Windows_exe_跑不起来时降级为archive() -> None:
    """Windows exe 在 Linux 上跑不了：必须降级到 archive 判据并给出明确手段。"""
    vp = _load()
    v = vp.verify(WIN_EXE)
    assert v.status == "pass", f"{v.status}: {v.failures}"
    assert v.method == "archive", f"Linux 上跑 Windows exe 应降级为 archive，实际 {v.method}"


# ── archive 分支单独验（真产物都完整，坏包只能用桩造）────────────────

def _force_archive(vp, info: dict, binary: Path):
    """把 verify 逼进 archive 分支：运行时探针返回 None，清单返回指定内容。"""
    vp.run_diag = lambda *a, **k: None            # 模拟「跑不起来」（跨架构）
    vp.read_archive = lambda *a, **k: info
    return vp.verify(binary)


def test_archive分支_缺schema判失败(tmp_path: Path) -> None:
    """包内没有 schema.sql 必须判失败。

    这条挡的是「漏收数据文件」的老事故（schema.sql 漏了 → 记忆库/产物库静默失效）。
    真 Windows/Linux 产物都是完整的，走到这条分支的只有坏包，所以用桩造。
    """
    vp = _load()
    probe = tmp_path / "fake-binary"
    probe.write_bytes(b"\x7fELF")
    v = _force_archive(vp, {"mkd": 37, "schema_sql": False, "error": None}, probe)
    assert v.status == "fail", f"缺 schema.sql 却报 {v.status}"
    assert v.method == "archive"
    assert any("schema.sql" in f for f in v.failures), v.failures


def test_archive分支_mkd数量不对判失败(tmp_path: Path) -> None:
    """包内模板数量不对（collect 只收到一部分）也要判失败。"""
    vp = _load()
    probe = tmp_path / "fake-binary"
    probe.write_bytes(b"\x7fELF")
    v = _force_archive(vp, {"mkd": 5, "schema_sql": True, "error": None}, probe)
    assert v.status == "fail", f"模板只有 5 个却报 {v.status}"
    assert any("mkd" in f or "模板" in f for f in v.failures), v.failures


def test_archive分支_清单读不了判unverified(tmp_path: Path) -> None:
    """清单也读不出来时必须是 `unverified`，不能因为计数是 0 就判 fail。"""
    vp = _load()
    probe = tmp_path / "not-a-pyinstaller-file"
    probe.write_bytes(b"hello")
    v = _force_archive(vp, {"mkd": 0, "schema_sql": False, "error": "格式不识别"}, probe)
    assert v.status == "unverified", f"读不了清单却报 {v.status} —— 『读不出来』≠『是 0 个』"
    assert v.failures == []


# ══════════════════════════════════════════════════════════════════
# 五、结构守卫：不许退回 strings 判内容
# ══════════════════════════════════════════════════════════════════

def test_build_linux不再用strings判内容() -> None:
    """`build_linux.sh` 不许再用 `strings` 判断数据文件在不在包里。

    两个死因都在这一行上：目标串不存在 + onefile 压缩。留着它只会持续误报，
    把人训练成「忽略自检」。

    **只查可执行行**：注释里引用旧代码是正常的（本次修复就把旧写法写在注释里
    解释了原因），不该被当成违规。
    """
    sh = (ROOT / "packaging" / "build_linux.sh").read_text(encoding="utf-8")
    bad = []
    for ln in sh.splitlines():
        code = ln.strip()
        if not code or code.startswith("#"):
            continue                       # 注释/空行跳过
        if "strings" in code and ("zw-enc2" in code or "CREATE TABLE" in code):
            bad.append(code)
    assert not bad, f"还在用 strings 判内容: {bad}"


def test_build_linux改为调用验证器() -> None:
    """自检必须真的**调用** `verify_package.py`，不能只是把旧检查删掉。

    ⚠️ 第一版只断言「文件里有 verify_package 这个词」，结果变异「把调用删掉、
    注释里仍留着这个词」照样绿（无牙）。必须查**可执行行**。
    """
    sh = (ROOT / "packaging" / "build_linux.sh").read_text(encoding="utf-8")
    calls = [
        ln.strip() for ln in sh.splitlines()
        if not ln.strip().startswith("#") and "verify_package.py" in ln
    ]
    assert calls, "自检没有真的调用 verify_package —— 删了旧检查却没补新的"
    # 三态必须落到三个不同分支上，否则「未验证」会被当成某一种
    assert "VERIFY_RC" in sh, "没接收验证器的退出码 —— 三态判决丢了"
    assert "0)" in sh and "1)" in sh and "*)" in sh, "退出码没有按三态分支处理"
