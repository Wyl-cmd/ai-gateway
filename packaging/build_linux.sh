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
#   ② 能被 --help / --version 之类触发（不真起服务，避免占端口）
#   ③ 关键数据文件确实打进包里 —— 破甲模板 37 个、schema.sql
#      这两样漏了不会报错，只会静默失效，必须在构建期查出来
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

  # ② 数据文件真的在包里
  #    onefile 模式下先解到临时目录，用 --debug 太吵，这里直接查二进制里有无特征串
  if strings "$OUT" 2>/dev/null | grep -q "zw-enc2-v1"; then
    ok "破甲模板内容在二进制里（找到 zw-enc2-v1 特征串）"
  else
    bad "二进制里找不到破甲模板特征串 —— pliny 模板可能没打进去！"
    VERIFY_FAIL=1
  fi

  if strings "$OUT" 2>/dev/null | grep -qi "CREATE TABLE"; then
    ok "schema.sql 在二进制里（找到 CREATE TABLE）"
  else
    warn "二进制里没找到 CREATE TABLE —— schema.sql 可能没打进去"
  fi

  # ③ 能起来（--version 或 --help，绝不起服务）
  dim "试着跑一下（10 秒超时，只验证解释器/依赖加载正常）…"
  if timeout 10 "$OUT" --help >/dev/null 2>&1; then
    ok "可执行文件能正常启动"
  else
    rc=$?
    if [ "$rc" -eq 124 ]; then
      warn "10 秒超时（可能直接进了 GUI/服务模式，属正常）"
    else
      warn "退出码 $rc（若 exe_entry 无 --help 分支则属正常，看下面手动验证）"
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
