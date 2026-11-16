"""打包产物内容验证器 —— 平台无关，Linux ELF 与 Windows PE 共用一套判据。

## 为什么需要它（2026-11-15）

`packaging/build_linux.sh --verify` 原来用这两行判断「数据文件有没有打进包里」：

```bash
strings "$OUT" | grep -q "zw-enc2-v1"      # 破甲模板
strings "$OUT" | grep -qi "CREATE TABLE"   # schema.sql
```

**两个独立缺陷，使它永远误报「自检未通过」**：

1. **目标字符串在本项目里根本不存在** —— `zw-enc2-v1` 是另一个项目（kxns 的 enc2
   协议串）的词，全仓只有 `build_linux.sh` 自己提到它。即使完全解包也搜不到。
2. **onefile 的 datas 是压缩存放的** —— 包内清单第 4 列压缩标记为 `1`，
   `schema.sql` 里 4 处 `CREATE TABLE` 在成品里 0 命中。压缩字节流里没有明文。

实测包是好的（`--diag`：mkd_on_disk=37 / templates_loaded=39 / schema_exists=True），
所以这是**假阴性**（false negative）——不会放坏包过关，但会让自检这个动作失去意义：
每次都报 ✗，人就会学会忽略它，将来真出问题也一样忽略。

## 判据分三级（不可跳级，不可混淆）

| 情况 | 手段 | 强度 |
|---|---|---|
| 能在本机跑起来 | 跑 `<binary> --diag`，读它写的 `ai-gateway-diag.log` | 最强：证明运行时真加载了模板 |
| 跑不起来（跨架构） | 读 PyInstaller 包内清单（`CArchiveReader`），数 `.mkd` 条目 | 次强：证明字节真在包里 |
| 两者都不行 | 返回 `unverified`，**绝不返回 pass** | —— |

第 3 级是关键：**「没验证」不等于「验证通过」，也不等于「验证失败」**。
本项目反复栽在「没看到坏结果就当成好结果」上，这里必须三态分明。

## 双端支持

PyInstaller 的 CArchive 格式在 PE 和 ELF 上一致，`CArchiveReader` 是纯 Python，
所以**在 Linux 上可以直接检查 Windows 的 `ai-gateway.exe`**（已实测）。
运行时探针（`--diag`）只能在对应平台上跑 —— Windows exe 需在 Windows 上验。

用法：

    python packaging/verify_package.py dist/ai-gateway-linux          # Linux
    python packaging\\verify_package.py dist\\ai-gateway.exe          # Windows
    python packaging/verify_package.py dist/ai-gateway.exe --json     # 给脚本消费

退出码：`0` 通过 / `1` 不通过 / `2` 未验证（三态，脚本才分得清）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

#: 源码树里的破甲模板数量（构建前脚本也会核对这个数）
EXPECTED_MKD = 37

#: `--diag` 写的日志文件名（与 packaging/exe_entry.py 的 `_diag()` 一致）
DIAG_LOG_NAME = "ai-gateway-diag.log"

#: 运行时探针的超时（秒）。onefile 首次启动要解包，给足时间
DIAG_TIMEOUT = 120


@dataclass
class DiagFacts:
    """`--diag` 日志里我们关心的四项。`None` = 那一行没读到。"""

    frozen: bool | None = None
    mkd_on_disk: int | None = None
    templates_loaded: int | None = None
    schema_exists: bool | None = None


@dataclass
class Verdict:
    """三态结论。`status` 只允许 `pass` / `fail` / `unverified`。"""

    status: str
    method: str
    detail: str = ""
    failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "pass"

    def render(self) -> str:
        """给人看的一行摘要。"""
        mark = {"pass": "✓", "fail": "✗", "unverified": "!"}.get(self.status, "?")
        tail = f" —— {'；'.join(self.failures)}" if self.failures else ""
        return f"  {mark} [{self.method}] {self.detail}{tail}"


def _to_bool(raw: str) -> bool | None:
    """`True`/`False` 文本转布尔；其余一律 `None`（读不到，不要瞎猜）。"""
    low = raw.strip().lower()
    if low in ("true", "1", "yes"):
        return True
    if low in ("false", "0", "no"):
        return False
    return None


def _to_int(raw: str) -> int | None:
    try:
        return int(raw.strip())
    except (TypeError, ValueError):
        return None


def parse_diag_log(text: str) -> DiagFacts:
    """把 `ai-gateway-diag.log` 的文本解析成 `DiagFacts`。

    日志是 `键=值` 逐行格式（见 `exe_entry._diag()`）。读不到的行留 `None` ——
    **「没读到」不等于「是 0」**，否则会把探针失败误判成模板缺失。
    """
    facts = DiagFacts()
    for line in text.splitlines():
        if "=" not in line:
            continue                     # traceback 之类直接跳过
        key, _, value = line.partition("=")
        key = key.strip()
        if key == "frozen":
            facts.frozen = _to_bool(value)
        elif key == "mkd_on_disk":
            facts.mkd_on_disk = _to_int(value)
        elif key == "templates_loaded":
            facts.templates_loaded = _to_int(value)
        elif key == "schema_exists":
            facts.schema_exists = _to_bool(value)
    return facts


def judge_diag(facts: DiagFacts, expected_mkd: int = EXPECTED_MKD) -> list[str]:
    """判运行时探针的事实。返回失败原因列表，**空列表 = 通过**。

    判据（三条都要满足）：
    - `mkd_on_disk` 等于预期数（模板文件真解到临时目录了）
    - `templates_loaded` 大于 0（真的解析成功了，不只是文件在）
    - `schema_exists` 为真（schema.sql 在包内）

    三条都是「探针没读到 → 判失败」：探针跑不通本身就是问题，
    不能因为「没看到坏结果」就放过（本项目反复栽在这里）。
    """
    failures: list[str] = []

    if facts.mkd_on_disk is None:
        failures.append("探针没读到 mkd_on_disk（--diag 未成功执行？）")
    elif facts.mkd_on_disk != expected_mkd:
        failures.append(f"包内模板 {facts.mkd_on_disk} 个 ≠ 预期 {expected_mkd} 个")

    if facts.templates_loaded is None:
        failures.append("探针没读到 templates_loaded")
    elif facts.templates_loaded <= 0:
        failures.append("模板解析出 0 个 —— 破甲会静默失效（8208 字掉到 131 字）")

    if facts.schema_exists is None:
        failures.append("探针没读到 schema_exists")
    elif not facts.schema_exists:
        failures.append("schema.sql 不在包内 —— 记忆库/产物库会静默失效")

    return failures


def read_archive(binary: Path) -> dict[str, object]:
    """读 PyInstaller 包内清单（跨平台，PE / ELF 通用）。

    返回 `{"mkd": <条目数>, "schema_sql": <bool>, "error": <str 或 None>}`。
    读不了时 `error` 非空、计数为 0 —— 调用方必须看 `error` 判断可信度，
    不能只看计数（否则「读不出来」会被当成「0 个模板」）。
    """
    out: dict[str, object] = {"mkd": 0, "schema_sql": False, "error": None}
    try:
        from PyInstaller.archive.readers import CArchiveReader
    except Exception as exc:                       # noqa: BLE001
        out["error"] = f"PyInstaller 不可用（{type(exc).__name__}: {exc}）"
        return out

    try:
        arch = CArchiveReader(str(binary))
    except Exception as exc:                       # noqa: BLE001
        out["error"] = f"不是 PyInstaller 包或格式不识别（{type(exc).__name__}: {exc}）"
        return out

    try:
        names = list(arch.toc)
        out["mkd"] = sum(1 for n in names if str(n).endswith(".mkd"))
        out["schema_sql"] = any(str(n).endswith("schema.sql") for n in names)
    except Exception as exc:                       # noqa: BLE001
        out["error"] = f"清单解析失败（{type(exc).__name__}: {exc}）"
    return out


def run_diag(binary: Path, timeout: int = DIAG_TIMEOUT) -> DiagFacts | None:
    """跑 `<binary> --diag` 并读它写的日志。跑不起来返回 `None`（交给下一级判据）。

    跨架构（Linux 上跑 Windows exe）、无执行权限、缺图形库等都会落到 `None` ——
    这是**正常的降级**，不是错误。
    """
    # **必须解析成绝对路径**：下面用 `cwd=binary.parent` 启动子进程，相对路径会
    # 变成 `dist/dist/ai-gateway-linux`（父目录又被当前目录叠加一次）→ 执行失败
    # → 静默降级到 archive，白丢最强的那一级判据（实测踩到，构建脚本正是传相对路径）。
    binary = Path(binary).resolve()
    if not binary.is_file():
        return None

    log = binary.parent / DIAG_LOG_NAME
    try:
        log.unlink()                       # 清掉上一次的，避免读到陈旧结果
    except OSError:
        pass

    try:
        subprocess.run(
            [str(binary), "--diag"],
            cwd=str(binary.parent),
            timeout=timeout,
            capture_output=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if not log.is_file():
        return None                        # 没写出日志 = 探针没跑成
    return parse_diag_log(log.read_text(encoding="utf-8", errors="replace"))


def verify(binary: Path, expected_mkd: int = EXPECTED_MKD) -> Verdict:
    """按三级判据验证产物，返回三态结论。"""
    binary = Path(binary)
    if not binary.is_file():
        return Verdict("unverified", "none", f"产物不存在：{binary}")

    # 一级：运行时真跑
    facts = run_diag(binary)
    if facts is not None:
        failures = judge_diag(facts, expected_mkd)
        detail = (
            f"mkd_on_disk={facts.mkd_on_disk} "
            f"templates_loaded={facts.templates_loaded} "
            f"schema_exists={facts.schema_exists}"
        )
        return Verdict("fail" if failures else "pass", "runtime-diag", detail, failures)

    # 二级：读包内清单（跨平台）
    info = read_archive(binary)
    if info["error"] is None:
        failures = []
        if info["mkd"] != expected_mkd:
            failures.append(f"包内 .mkd {info['mkd']} 个 ≠ 预期 {expected_mkd} 个")
        if not info["schema_sql"]:
            failures.append("包内没有 schema.sql")
        detail = f"包内 .mkd={info['mkd']} schema.sql={'在' if info['schema_sql'] else '缺'}"
        return Verdict("fail" if failures else "pass", "archive", detail, failures)

    # 三级：都不行 —— 明确「未验证」
    return Verdict(
        "unverified",
        "none",
        f"运行时探针不可用（跨架构或无法执行）；包内清单也读不了（{info['error']}）",
    )


#: 三态到进程退出码的映射。**脚本靠退出码分辨三态**，不能只靠人读输出。
EXIT_CODES = {"pass": 0, "fail": 1, "unverified": 2}


def main(argv: list[str] | None = None) -> int:
    """命令行入口：`python packaging/verify_package.py <产物路径>`。"""
    args = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in args
    args = [a for a in args if a != "--json"]
    expected = EXPECTED_MKD
    if "--expected-mkd" in args:
        idx = args.index("--expected-mkd")
        try:
            expected = int(args[idx + 1])
            del args[idx:idx + 2]
        except (IndexError, ValueError):
            print("用法错误：--expected-mkd 需要一个整数", file=sys.stderr)
            return 2

    if len(args) != 1:
        print("用法：python packaging/verify_package.py <产物路径> [--json] [--expected-mkd N]",
              file=sys.stderr)
        return 2

    verdict = verify(Path(args[0]), expected_mkd=expected)

    if as_json:
        print(json.dumps({
            "status": verdict.status,
            "method": verdict.method,
            "detail": verdict.detail,
            "failures": verdict.failures,
        }, ensure_ascii=False))
    else:
        print(verdict.render())

    return EXIT_CODES[verdict.status]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
