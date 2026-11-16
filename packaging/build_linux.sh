#!/usr/bin/env bash
# AI 网关 Linux 单文件可执行 —— 一键构建
#
# 用法（项目根目录执行）：
#     bash packaging/build_linux.sh              # 正式构建
#     bash packaging/build_linux.sh --verify      # 构建 + 自检（推荐）
#     bash packaging/build_linux.sh --clean       # 清掉旧的 build/dist 再构建
#
# 产物：dist/ai-gateway-linux
#
# 自检做什么（--verify）：
#   ① 产物存在且可执行
#   ② 数据文件真的在包里（交给 packaging/verify_package.py，三态判定）：
#        能跑 → 跑 `--diag` 看运行时真加载了几个模板（最强）
#        跑不了（跨架构）→ 读 PyInstaller 包内清单数 .mkd / schema.sql（次强）
#        都不行 → 明确打「未验证」，绝不假装通过
#      为什么不用 strings 搜明文：onefile 的 datas 是压缩存放的（包内清单压缩标记=1），
#      压缩字节流里没有明文，`strings | grep` 必然搜不到 → 恒误报（2026-11-15 修）。
#   ③ 能正常启动（跑 `--diag`，无头安全、不占端口、不碰 GUI）
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"

GREEN=$'\033[32m'; RED=$'\033[31m'; YELLOW=$'\033[33m'; DIM=$'\033[2m'; NC=$'\033[0m'
ok()   { printf "%s  ✓%s %s\n" "$GREEN" "$NC" "$*"; }
bad()  { printf "%s  ✗%s %s\n" "$RED"   "$NC" "$*"; }
warn() { printf "%s  !%s %s\n" "$YELLOW" "$NC" "$*"; }
dim()  { printf "%s    %s%s\n" "$DIM" "$*" "$NC"; }

VERIFY=0
CLEAN=0
for arg in "$@"; do
  case "$arg" in
    --verify) VERIFY=1 ;;
    --clean)  CLEAN=1 ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) bad "未知参数: $arg"; exit 2 ;;
  esac
done

# ── 找 Python（优先项目 venv）────────────────────────────────
PY=""
for cand in ".venv/bin/python" "venv/bin/python" "python3"; do
  if [ -x "$cand" ] || command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c "import sys" 2>/dev/null; then PY="$cand"; break; fi
  fi
done
[ -n "$PY" ] || { bad "找不到可用的 Python"; exit 1; }
dim "Python: $($PY -c 'import sys;print(sys.version.split()[0], sys.executable)')"

# ── 依赖检查 ─────────────────────────────────────────────────
$PY -c "import PyInstaller" 2>/dev/null || {
  warn "缺 PyInstaller，尝试安装（pip install -e '.[build]'）"
  $PY -m pip install -q -e ".[build]" || {
    bad "装不上。手动执行: $PY -m pip install pyinstaller pillow"
    exit 1
  }
}
dim "PyInstaller: $($PY -c 'import PyInstaller;print(PyInstaller.__version__)')"

# ── 构建前必须自查的两件东西（漏了会静默失效）─────────────────
echo
dim "构建前检查数据文件是否存在…"
MKD_COUNT=$(find src/gateway/jailbreak/data/pliny -name '*.mkd' 2>/dev/null | wc -l)
if [ "$MKD_COUNT" -eq 0 ]; then
  bad "破甲模板 0 个！src/gateway/jailbreak/data/pliny/*.mkd 不存在"
  bad "  打出来的包会启动正常但破甲静默失效（8208 字 → 131 字，不报错）"
  exit 1
fi
ok "破甲模板 $MKD_COUNT 个"
[ -f src/gateway/storage/schema.sql ] && ok "schema.sql 在" || { bad "缺 storage/schema.sql"; exit 1; }

# ── 清理 ─────────────────────────────────────────────────────
if [ "$CLEAN" -eq 1 ]; then
  dim "清理 build/ dist/ …"
  rm -rf build dist
fi

# ── 构建 ─────────────────────────────────────────────────────
echo
dim "开始构建（首次约 1~3 分钟）…"
$PY -m PyInstaller packaging/ai-gateway-linux.spec \
    --noconfirm --distpath dist --workpath build --log-level WARN

OUT="dist/ai-gateway-linux"
[ -f "$OUT" ] || { bad "构建失败：$OUT 不存在"; exit 1; }
chmod +x "$OUT"

SIZE=$(du -h "$OUT" | cut -f1)
echo
ok "构建完成: $OUT  ($SIZE)"

# ── 自检 ─────────────────────────────────────────────────────
if [ "$VERIFY" -eq 1 ]; then
  echo
  dim "自检中…"

  # ① 能执行
  file "$OUT" | grep -q ELF && ok "产物是 ELF 可执行文件" || warn "产物不是 ELF（奇怪）"

  # ② 数据文件真的在包里 —— 交给 verify_package.py（Linux/Windows 同一套判据）
  #
  # 为什么不用 strings：
  #   原来这里是 `strings "$OUT" | grep -q "zw-enc2-v1"` / `grep -qi "CREATE TABLE"`，
  #   两个独立缺陷让它恒误报（2026-11-15 实测）：
  #     ① `zw-enc2-v1` 在本项目里根本不存在（是另一个项目的串），全仓只有本脚本提到它
  #     ② onefile 的 datas 是压缩存放的（包内清单压缩标记=1），
  #        schema.sql 里 4 处 CREATE TABLE 在成品里 0 命中 —— 压缩字节流里没有明文
  #   恒误报的后果不是「多按一次跳过」，是把人训练成忽略自检，将来真漏了也一样忽略。
  #
  # 为什么改判运行时：`--diag`（packaging/exe_entry.py）会真的把模板读一遍并写日志，
  # 比「字节在不在包里」更接近实际可用性。跨架构跑不起来时才降级去读包内清单。
  echo
  dim "验证数据文件（--diag 运行时探针 → 包内清单 → 未验证）…"
  $PY packaging/verify_package.py "$OUT" && VERIFY_RC=0 || VERIFY_RC=$?
  case "$VERIFY_RC" in
    0) ok "数据文件验证通过" ;;
    1) bad "数据文件验证不通过 —— 见上面 ✗ 行的原因"; VERIFY_FAIL=1 ;;
    *) warn "产物未能验证（既跑不起来也读不了包）—— 这既不是通过也不是失败" ;;
  esac

  # ③ 能起来（`--diag` 是无头安全的真探针，绝不起服务、不碰 GUI）
  #
  # 原来这里是 `timeout 10 "$OUT" --help`，入口是 GUI（exe_entry → run_gui）且没有
  # `--help` 分支 → Qt 找不到 xcb 就 SIGABRT(134) → 固定打一行「已中止 / 退出码 134」。
  # 那是**环境造成的假警报**，和上面 strings 那两处同一性质：吵久了就没人看自检了。
  # `--diag` 会在无显示器环境下正常跑完并退出（第 ② 项已经用它跑过一次，这里同样能用）。
  dim "再确认一次能正常启动（--diag，10 秒超时，不占端口）…"
  if timeout 10 "$OUT" --diag >/dev/null 2>&1; then
    ok "可执行文件能正常启动并完成自检"
  else
    rc=$?
    if [ "$rc" -eq 124 ]; then
      warn "10 秒超时没跑完（onefile 首次解包可能偏慢）"
    else
      bad "--diag 退出码 $rc —— 产物起来就崩，不能发布"
      VERIFY_FAIL=1
    fi
  fi

  echo
  dim "手动验证建议："
  dim "  $OUT                    # 起服务/GUI"
  dim "  curl http://127.0.0.1:8787/health"
  if [ "${VERIFY_FAIL:-0}" = "1" ]; then
    echo; bad "自检未通过 —— 见上面的 ✗ 项"; exit 1
  fi
  echo; ok "自检通过"
fi
