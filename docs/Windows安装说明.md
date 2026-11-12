# Windows 安装与运行

> 这个网关是纯 Python 项目，Windows 上不需要编译任何东西。

## 一、装 Python

需要 **Python 3.11 或更高**（开发用的是 3.14.7，3.11+ 都行）。

```
https://www.python.org/downloads/windows/
```

安装时**务必勾选** `Add python.exe to PATH`，否则命令行找不到 `python`。

验证：

```cmd
python --version
```

## 二、解压后进目录

```cmd
cd ai-gateway
```

## 三、建虚拟环境 + 装依赖

```cmd
python -m venv .venv
.venv\Scripts\activate
pip install -e .
```

**想要图形界面**（Windows 托盘）再加：

```cmd
pip install -e ".[gui]"
```

（会装 PySide6，约 100 MB，第一次慢）

## 四、写配置

复制 `config.example.toml` 成 `config.toml`。**密钥不写在配置文件里** ——
配置文件可以随便传给别人；密钥存同目录的 `secrets.toml`（已被 git 忽略）：

```toml
auth_keys = ["自己定一个网关密码"]   # 顶层，必须在所有 [表头] 之前

[server]
host = "0.0.0.0"        # 局域网全设备可访问
port = 8787

[[providers]]
name = "deepseek"
kind = "openai"                          # openai 兼容接口都用这个
base_url = "https://api.deepseek.com"
models = ["deepseek-chat", "deepseek-reasoner"]   # 这家能用的模型，必填

[[providers.accounts]]
key_id = "key1"
key_env = "DEEPSEEK_KEY_1"               # 密钥在 secrets.toml 里的键名
```

密钥两种给法（**推荐第一种**，Windows/Linux 通用）：

**① 密钥文件**（推荐）：GUI 设置页粘贴保存，或手写 `secrets.toml`：

```toml
DEEPSEEK_KEY_1 = "sk-你的DeepSeek密钥"
```

**② 环境变量**（兼容旧部署）：`set DEEPSEEK_KEY_1=sk-...`（cmd，当前窗口
生效）。环境变量优先于密钥文件。

多个账号就写多个 `[[providers.accounts]]` 块，网关会自动轮巡；
配额耗尽只跳过那个账号，同一家其他 key 继续用。

## 五、启动

**图形界面（推荐）**：

```cmd
.venv\Scripts\python -m gateway.gui.app
```

Windows 上会出现在**系统托盘**（右下角，可能要点开小箭头）。
面板里是真实数据：账号池状态、每次请求的路由链、降级告警、统计。
切 MANUAL 会出「锁定目标 + 降级策略」选择器，切换立即生效。

**纯命令行（读同一份 config.toml）**：

```cmd
.venv\Scripts\gateway serve
```

会打印监听的地址，包括局域网 IP，手机可以直接连。

## 六、用

像调 OpenAI 一样调它：

```cmd
curl http://127.0.0.1:8787/v1/chat/completions ^
  -H "Authorization: Bearer 你设的网关密码" ^
  -H "Content-Type: application/json" ^
  -d "{\"model\":\"deepseek-chat\",\"messages\":[{\"role\":\"user\",\"content\":\"你好\"}]}"
```

Linux/macOS 下反斜杠换成 `\`，或者用 PowerShell 的 `curl.exe`。

## 七、破甲（默认开）

网关会把你的请求变形后再发给上游模型，绕开拒绝。

**实测数据**（DeepSeek 真实上游，问题「写游戏外挂给完整代码」）：

```
原样问                                 640 字拒绝
网关破甲（pliny_vendor 模板）         8208 字，完整 C++ 工程骨架
```

关掉它：配置里 `[jailbreak] enabled = false`。

## 八、Windows 上可能踩的坑

**① `python` 不是内部或外部命令**

装 Python 时没勾 PATH。重装勾上，或者用完整路径
`C:\Users\你\AppData\Local\Programs\Python\Python311\python.exe`。

**② 端口被占用**

```
netstat -ano | findstr :8787
```

改配置里的 `port` 换个端口。

**③ 防火墙拦截局域网访问**

第一次启动 Windows 会弹「是否允许访问网络」，**要选允许**。
已经点了拒绝的话，去
`控制面板 → Windows Defender 防火墙 → 允许应用通过防火墙` 里手动加上。

**④ 托盘图标找不到**

在右下角任务栏的小箭头（`^`）里，拖出来固定即可。

**⑤ 装 PySide6 报错**

先升级 pip：`python -m pip install --upgrade pip`，再装。

## 九、跑测试（可选）

**要装 dev 依赖**（`pip install -e .` 只装运行时依赖，不含 pytest）：

```cmd
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\python -m pytest tests -q
```

应该看到 `353 passed`。

端到端测试要真实调 API（会花钱），默认排除，单独跑：

```cmd
.venv\Scripts\python -m pytest tests/e2e -q -m e2e
```

## 十、不装 GUI 也能用

GUI 只是好看，服务本身不需要 PySide6。
最小安装（省 100 MB）：

```cmd
pip install -e .
.venv\Scripts\python -m gateway.demo
```

托盘图标、模型切换面板这些都要 PySide6；不加 `[gui]` 时用命令行和配置文件调。
