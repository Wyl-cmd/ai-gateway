# 打包 Windows 免安装 exe

> 产物：`dist/ai-gateway.exe`（单文件，约 54 MB，含 PySide6）
> 和 `dist/ai-gateway-0.1.0-win64.zip`（exe + 配置模板，发给别人用这个）

## 一、准备

```cmd
pip install -e ".[gui,build]"
python tools\make_icon.py          :: 重新生成 assets/ai-gateway.ico（可选）
```

## 二、构建

```cmd
.venv\Scripts\python -m PyInstaller packaging\ai-gateway.spec ^
    --noconfirm --distpath dist --workpath build

:: 顺手重建 pip 安装包
.venv\Scripts\python -m build --wheel
```

## 三、验收（每次打包必做）

```cmd
:: ① 两个产物里的模板和 schema 数量必须对
.venv\Scripts\python -c "import zipfile; w=zipfile.ZipFile('dist/ai_gateway-0.1.0-py3-none-any.whl'); print(len([n for n in w.namelist() if n.endswith('.mkd')]), [n for n in w.namelist() if n.endswith('schema.sql')])"

:: ② exe 自检（模板目录 / 模板数 / schema / frozen 状态）
dist\ai-gateway.exe --diag
type dist\ai-gateway-diag.log
```

`--diag` 期望输出：`mkd_on_disk=37`、`templates_loaded=39`、
`deepseek_templates=6`、`schema_exists=True`。

**③ 真实双击路径测试**：用 `Start-Process` 或资源管理器双击启动
（不要从 Git Bash 直接跑 —— 有控制台句柄会掩盖 windowed 模式的问题），
然后访问 `http://127.0.0.1:8787/health`。

## 四、三个已知坑（都实测踩过）

**① windowed exe 里 `sys.stdout` 是 None**
uvicorn 的日志格式化器构造时调 `sys.stdout.isatty()` →
`ValueError: Unable to configure formatter 'default'` → 双击直接崩。
解决：`packaging/exe_entry.py` 的 `_ensure_std_streams()` 把标准流
兜底到 `ai-gateway.log`（必须在导入 uvicorn 之前调用）。

**② `schema.sql` 必须进包**
`storage/repo.py` 靠它建表。漏了不报错，只表现为「记忆/产物库静默失效」
（破甲排序退化）。pyproject 的 `package-data` 和 spec 的
`collect_data_files("gateway")` 必须都包含它 —— 只挑 `*.mkd` 会漏。

**③ uvicorn 的协议实现是字符串动态导入**
PyInstaller 静态分析看不见，必须写进 spec 的 `hiddenimports`，
否则启动时报 `Could not import module "uvicorn.protocols.http.h11_impl"`。

## 五、发布

GitHub Release 附件：`ai-gateway.exe` + `ai-gateway-0.1.0-win64.zip` +
`ai_gateway-0.1.0-py3-none-any.whl` + `ai_gateway-0.1.0.tar.gz`。
`dist/` 与 `build/` 不进 git（已在 .gitignore）。
