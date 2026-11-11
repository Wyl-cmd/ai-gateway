# Windows 桌面版 + 多层路由 + 思考等级 方案

> **本文档是 v2 方向变更的根，取代原 v1 的「Linux CLI」形态**
> 依据：Master 2026-11-11 提出的五条新需求
> 所有「各家参数形式」均来自官方文档实测（OpenAI / Anthropic / Gemini / DeepSeek），非记忆

---

# 一、五条需求逐条裁决

| # | 需求（Master 原话要点） | 能做吗 | 影响 |
|---|---|---|---|
| ① | 同一供应商有多家公司的同款/不同代模型，**按模型型号区分**，轮巡、切换、降级 | ✅ 能 | 路由层从「单维度」→「**三层寻址**」 |
| ② | 切换模式（型号间切 / 供应商间切 / 怎么切）**都要能在图形界面设置** | ✅ 能 | 建 GUI + 策略配置界面 |
| ③ | 最终适配 **Windows 电脑** | ✅ 能 | **平台变更**：Linux 服务 → Windows 桌面应用 |
| ④ | GUI 显示：模式(auto/manual)、供应商、当前模型、手动模式下降级会降到哪、当前选择全貌 | ✅ 能 | GUI 要有实时状态推送 |
| ⑤ | 模型有不同**思考深度/方式**（低/中/高档），聚合能否满足不同 agent 调配**不同思考等级** | ⚠️ **能做，但不能 100% 统一** | 必须做**归一化层 + 能力声明表** |

**两个最大变更**：**平台换 Windows**、**形态换桌面 GUI**。这两条改变技术栈与打包方式。

---

# 二、① 三层寻址模型（核心重构）

## 2.1 原来的问题

原方案只有两层：`provider/account/model`。这**无法表达** Master 说的场景：

```
同一款模型 gpt-4o，可能由多家提供：
  - OpenAI 官方
  - Azure OpenAI（同款模型、不同端点、不同配额、可能不同版本）
  - 第三方中转 A（便宜但可能限流/量化）
  - 第三方中转 B
```

原方案会把它当成「4 个不同 provider 的同一个 model」，**没法表达「我要 gpt-4o 这个型号，
供应商随便，但我优先官方」**。

## 2.2 新模型：四层

```
┌─ 模型型号 model_family ─────────────────────────────────────┐
│  这一层回答「我要什么能力」                                    │
│  gpt-4o / gpt-5 / claude-sonnet-4 / deepseek-chat / gemini-3-pro│
│                                                              │
│      ↓ 一个型号可由多个供应商提供（同款不同源）                 │
├─ 供应商 provider ───────────────────────────────────────────┤
│  这一层回答「从谁那里买」                                      │
│  openai-official / azure-eastus / relay-a / relay-b / self-host│
│  每个供应商带：协议类型、base_url、计价、能力差异、优先级        │
│                                                              │
│      ↓ 一个供应商下多个账号                                    │
├─ 账号 account ──────────────────────────────────────────────┤
│  这一层回答「用哪个 key」                                      │
│  key1 / key2 / key3（配额、限流、冷却状态各自独立）             │
│                                                              │
│      ↓ 正交维度（不在这条链上，是横切的）                       │
└─ 思考等级 thinking_level ───────────────────────────────────┘
   off / low / medium / high / max  → 按「供应商+型号」映射成各自参数
```

**关键设计**：
- **`model_family` 是「能力标识」**，不是「API 里的 model 字符串」。
  一个 family 在不同 provider 下可能有不同的实际 model 名（Azure 的 `deployment-name`、
  中转的别名）。
- **provider 带「能力差异声明」**：同一个 gpt-4o，官方支持 128k 上下文，
  某中转可能只给 32k；官方支持 `reasoning_effort`，中转可能直接忽略。

## 2.3 数据模型（新增 `model_family` 与 `provider_capability`）

```sql
-- 模型型号（能力标识，用户面向的）
CREATE TABLE IF NOT EXISTS model_family (
    family          TEXT PRIMARY KEY,      -- "gpt-4o" / "deepseek-chat"
    display_name    TEXT NOT NULL,
    tier            TEXT,                  -- flagship / standard / lite / reasoning
    generation      INTEGER,               -- 第几代（同家族降级用：5 → 4 → 3.5）
    context_window  INTEGER,               -- 声明值（provider 可能更小，取 min）
    supports_thinking INTEGER NOT NULL DEFAULT 0,
    created_at      INTEGER NOT NULL
);

-- 供应商在某型号上的接入点（family × provider 的笛卡尔积里的有效组合）
CREATE TABLE IF NOT EXISTS provider_route (
    route_id        TEXT PRIMARY KEY,      -- "openai-official/gpt-4o"
    family          TEXT NOT NULL,
    provider        TEXT NOT NULL,
    upstream_model  TEXT NOT NULL,         -- 实际发给上游的 model 字符串
    -- 能力差异（NULL = 沿用 family 声明）
    context_window  INTEGER,               -- 该供应商实际给多少
    max_output      INTEGER,
    thinking_mode   TEXT,                  -- 见 §三
    supports_stream  INTEGER NOT NULL DEFAULT 1,
    supports_tools   INTEGER NOT NULL DEFAULT 1,
    forward_extra    TEXT,                 -- JSON：该供应商特有但需透传的字段
    priority        INTEGER NOT NULL DEFAULT 100,  -- 越小越优先（同 family 内）
    enabled         INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (family) REFERENCES model_family(family)
);
CREATE INDEX IF NOT EXISTS idx_provider_route_family ON provider_route(family, priority);
```

**为什么 `upstream_model` 单独一列**：Azure 的 `deployment-name` 可以随便起名，
中转可能把 `gpt-4o` 叫 `gpt-4o-relay`。**逻辑型号 ≠ 上游字符串**，混在一起会让降级逻辑看不懂。

---

# 三、⑤ 思考等级归一化（最大难点）

## 3.1 现实：各家参数形式完全不同（官方文档实测）

| 家 / 代际 | 参数 | 取值形式 | 备注 |
|---|---|---|---|
| **OpenAI** o 系列 / GPT-5 | `reasoning_effort` | `minimal` / `low` / `medium` / `high` | 4 档枚举 |
| **Anthropic** Claude ≤4.6 | `thinking: {type:"enabled", budget_tokens:N}` | **整数 token** | extended thinking |
| **Anthropic** Claude 4.7+ | `thinking:{type:"adaptive"}` + `effort` | 枚举 | **extended thinking 已是 legacy** |
| **Gemini 2.5** | `thinking_budget` | **整数**（0 = 关） | |
| **Gemini 3+** | thinking **level** | 枚举 | **换了一套，2.5 和 3 不兼容** |
| **Qwen** | `enable_thinking` | **bool** | 只有开/关，没有档位 |
| **DeepSeek** | thinking mode | 独立参数 | 见官方 guides/thinking_mode |

**结论：不存在「统一档位」。** 所以必须做两件事：

1. **统一档位定义**（我们对外的口径）
2. **每模型的映射表**（我们 → 上游）

## 3.2 统一档位（对外口径，5 档）

```python
ThinkingLevel = Literal["off", "low", "medium", "high", "max"]
```

**为什么是这 5 档**（不是 4 也不是 6）：覆盖所有上游的取值空间 ——
`off`（关）/ `low`（最快最省）/ `medium`（默认）/ `high`（多数模型最高档）/ `max`（只有部分支持）。
再多的档位没有上游能兑现。

## 3.3 映射表（每一行都是「实测过的参数形式」）

```toml
# ── 思考等级映射 ──
# 按「上游协议类型」定义，不是按 provider 名 —— 同协议的所有 provider 共用

[thinking_map.openai_reasoning]
# OpenAI o 系列 / GPT-5 的 reasoning_effort（4 档枚举）
off    = { omit = true }                 # 不支持关闭，直接不发这个字段
low    = { reasoning_effort = "low" }
medium = { reasoning_effort = "medium" }
high   = { reasoning_effort = "high" }
max    = { reasoning_effort = "high" }   # 上游只有 4 档，max 退化到 high

[thinking_map.anthropic_budget]
# Claude ≤4.6 的 budget_tokens（整数）
off    = { thinking = { type = "disabled" } }
low    = { thinking = { type = "enabled", budget_tokens = 1024 } }
medium = { thinking = { type = "enabled", budget_tokens = 4096 } }
high   = { thinking = { type = "enabled", budget_tokens = 16384 } }
max    = { thinking = { type = "enabled", budget_tokens = 32000 } }
# 约束：budget_tokens 必须 < max_tokens

[thinking_map.anthropic_adaptive]
# Claude 4.7+ 的 adaptive + effort
off    = { thinking = { type = "disabled" } }
low    = { thinking = { type = "adaptive" }, effort = "low" }
medium = { thinking = { type = "adaptive" }, effort = "medium" }
high   = { thinking = { type = "adaptive" }, effort = "high" }
max    = { thinking = { type = "adaptive" }, effort = "high" }

[thinking_map.gemini_budget]
# Gemini 2.5 的 thinking_budget（整数）
off    = { thinking_budget = 0 }
low    = { thinking_budget = 1024 }
medium = { thinking_budget = 8192 }
high   = { thinking_budget = 24576 }
max    = { thinking_budget = 32768 }

[thinking_map.gemini_level]
# Gemini 3+ 的 level（换了一套）
off    = { thinking_level = "off" }
low    = { thinking_level = "low" }
medium = { thinking_level = "medium" }
high   = { thinking_level = "high" }
max    = { thinking_level = "high" }

[thinking_map.boolean]
# Qwen 等只有开关的
off    = { enable_thinking = false }
low    = { enable_thinking = true }
medium = { enable_thinking = true }
high   = { enable_thinking = true }
max    = { enable_thinking = true }
```

## 3.4 「不同模型分类不一样」怎么处理（Master 这条问得最准）

**事实**：有的模型 4 档、有的 2 档（开/关）、有的连续（token 数）。

**处理三步**：

### ① 每个 `provider_route` 声明自己的 `thinking_mode`

```sql
thinking_mode TEXT   -- "openai_reasoning" | "anthropic_budget" | "anthropic_adaptive"
                     -- | "gemini_budget" | "gemini_level" | "boolean" | NULL(不支持)
```

`NULL` = 该模型完全不支持思考等级 → 任何档位请求都**忽略且不报错**，但在响应里
`thinking_applied=false`（**明确告知降级，不假装生效**）。

### ② 降级时按「实际能兑现的档位」算，不是按请求的档位

```
用户请求 high
  → 选中的 route 是 boolean 型（只有开关）
  → 实际只能用 "on"
  → 响应里写 thinking_requested=high, thinking_applied=high_equivalent_boolean,
     thinking_degraded=true
```

**这是关键**：不告诉用户「你要的 high 在这家只能做到开关」，用户会以为拿到了高档思考。

### ③ 跨模型降级时，档位往上取

如果从 `gpt-5`（4 档）降到 `qwen`（boolean），`medium` → `true`（够用即可，不硬凑）。
如果从 `qwen` 升到 `gpt-5`，`true` → `medium`（**不默认给 high**，避免突然变慢变贵）。

## 3.5 不同 agent 调配不同思考等级（Master 这个需求怎么满足）

**入口设计**（三种，按优先级）：

```http
# ① 显式指定（最优先）
X-Gateway-Thinking: high

# ② 按用途预设（agent 声明"我在干什么"，网关选档）
X-Gateway-Purpose: code_gen | quick_qa | deep_analysis | tool_call

# ③ 不指定 → 用该模型的默认档（默认 medium）
```

**用途预设映射**（可配）：

| purpose | thinking | 理由 |
|---|---|---|
| `quick_qa` | `off` 或 `low` | 闲聊、简单问答，思考纯浪费 |
| `tool_call` | `low` | 工具调用要快，不需要长思考 |
| `code_gen` | `medium` | 写代码要一定推理 |
| `deep_analysis` | `high` | 漏洞分析、架构设计 |

**为什么需要 ②**：agent 自己最清楚「这一步要不要深想」。让 agent 声明用途，
比让它猜「high 会不会太慢」更自然。

---

# 四、② 四维切换策略（GUI 可配）

## 4.1 四个独立的切换维度

| 维度 | 触发 | 动作 | 例子 |
|---|---|---|---|
| **A 账号轮巡** | 配额耗尽 / 限流 / 该 key 冷却 | 同 provider 换 key | `openai:key1` → `openai:key2` |
| **B 供应商切换** | 整个 provider 不可用 | 同 family 换 provider | `openai-official` → `azure` → `relay-a` |
| **C 型号降级** | 同 family 所有 provider 都挂 | 换成更低一代/更小的型号 | `gpt-5` → `gpt-4o` → `gpt-4o-mini` |
| **D 思考降级** | 超时 / 预算不足 / 显式配置 | 降低思考档 | `high` → `medium` → `low` |

## 4.2 关键：**顺序可配**（这是 Master ② 的核心要求）

```toml
[fallback]
# 四个维度的尝试顺序（从左到右）
order = ["account", "provider", "thinking", "model"]
#            ↑ A          ↑ B         ↑ D          ↑ C

# 每一维的开关
[fallback.account]
enabled = true
max_attempts = 3          # 最多换 3 个 key

[fallback.provider]
enabled = true
max_attempts = 2          # 最多换 2 家

[fallback.thinking]
enabled = true
ladder = ["high", "medium", "low"]   # 只在超时/预算触发时降

[fallback.model]
enabled = true
max_attempts = 2
# 型号降级链（显式指定，不靠"自动猜哪代低"）
chain = ["gpt-5", "gpt-4o", "gpt-4o-mini"]
```

**两种典型配置**：

```toml
# 配置甲：质量优先（先换账号/家，最后才降能力）
order = ["account", "provider", "model", "thinking"]

# 配置乙：省钱优先（先降思考，再换号）
order = ["thinking", "account", "provider", "model"]
```

**为什么必须可配**：这两个顺序的语义完全不同 ——
甲是「宁可贵也要保住能力」，乙是「宁可降能力也要省钱」。**没有绝对正确的顺序，只有场景**。

## 4.3 GUI 里怎么配

「切换策略」页：
```
切换顺序（拖拽排序）
  1. [账号轮巡]  ≡      最多 3 次
  2. [供应商切换] ≡     最多 2 家
  3. [型号降级]  ≡      链: gpt-5 → gpt-4o → gpt-4o-mini  [编辑]
  4. [思考降级]  ≡      链: high → medium → low          [编辑]
     ☑ 账号轮巡  ☑ 供应商切换  ☑ 型号降级  ☑ 思考降级

预设: [质量优先] [省钱优先] [自己配]
```

---

# 五、③④ Windows 桌面应用 + GUI

## 5.1 技术栈决策：**Python + PySide6**

| 候选 | 后端复用 | Windows 原生感 | 打包 | 体积 |
|---|---|---|---|---|
| **PySide6（Qt）** ✅ | **100%**（后端全是 Python） | 好 | PyInstaller 单 exe | ~80 MB |
| Tauri（Rust+Web） | 0%（要重写后端或做 sidecar） | 最好 | 天然小 | ~10 MB |
| Electron + Web | 0%（要起 Python sidecar） | 一般 | electron-builder | ~150 MB |
| 纯 Web UI + 浏览器 | 100% | **不是桌面应用** | 无需打包 | 0 |

**选 PySide6 的决定性理由**：后端（路由/破甲/转发/账本）已经全是 Python，
用 Qt 可以**同一个进程内直接调用**，不需要 sidecar 进程间通信。Tauri/Electron 虽然界面更好看，
但要把 Python 后端当子进程管，多一层故障面。

**代价**：Qt 界面没那么"现代"。但这是自用工具，**能看清状态 > 好看**。

## 5.2 主界面布局

```
┌──────────────────────────────────────────────────────────────────┐
│ AI 网关                          [● AUTO 手动]   ● 运行中  12 路  │
├──────────────────────────────────────────────────────────────────┤
│ 当前路由（Master ④ 要求全显示）                                    │
│ ┌──────────────────────────┬────────────────────────────────────┐│
│ │ 模式      AUTO（自动）    │ 供应商   DeepSeek 官方              ││
│ │ 型号      deepseek-chat   │ 账号     key1（额度 ∞）             ││
│ │ 思考等级  中 medium       │ 实际参数 reasoning_effort=medium    ││
│ │ 延迟      320 ms          │ 本次花费 $0.0004                    ││
│ ├──────────────────────────┴────────────────────────────────────┤│
│ │ 降级链（手动模式下"会降到哪"也在这里显示）                        ││
│ │   deepseek-chat(官方,key1) → deepseek-chat(官方,key2)          ││
│ │   → deepseek-chat(中转A) → deepseek-flash(官方) → 无可用        ││
│ └───────────────────────────────────────────────────────────────┘│
├──────────────────────────────────────────────────────────────────┤
│ 路由表                                        [筛选: 全部型号 ▾]  │
│ 型号            供应商   账号  思考  健康  配额   延迟  操作       │
│ deepseek-chat   官方     key1  中    ●正常  ∞     320ms [停用]    │
│ deepseek-chat   官方     key2  中    ●正常  ∞     350ms [停用]    │
│ deepseek-chat   中转A    key1  中    ▲冷却  ∞     890ms [启用]    │
│ deepseek-flash  官方     key1  低    ●正常  ∞     180ms [停用]    │
│ gpt-5           官方     key1  高    ✕无key —      —    [配置]    │
├──────────────────────────────────────────────────────────────────┤
│ [切换策略] [思考映射] [预算] [破甲] [请求日志] [设置]              │
└──────────────────────────────────────────────────────────────────┘
```

**手动模式下**，「当前路由」卡片变成：
```
│ 模式      手动（锁定）                                            │
│ 请求       openai-official / gpt-4o                              │
│ 实际       openai-official / gpt-4o        ✅ 就是你要的           │

# 若发生降级：
│ 请求       openai-official / gpt-4o                              │
│ 实际       deepseek-official / deepseek-chat  ⚠️ 已降级            │
│ 原因       openai 全部账号额度耗尽，按顺序降到 deepseek-chat         │
│ 会降到哪   deepseek-chat → deepseek-flash → 无可用   ← Master ④ 要求│
```

## 5.3 实时状态推送

后端 → GUI 用 **Qt Signal**（同进程，零网络开销）：

```python
class GatewayBridge(QObject):
    route_changed = Signal(dict)      # 当前路由变化
    route_state   = Signal(list)      # 路由表健康变化
    request_done  = Signal(dict)      # 每次请求完成（延迟/花费/破甲）
    log_line      = Signal(str)
```

**为什么不用轮询**：轮询 1 秒一次会让「正在发请求时」的界面滞后，
而 Signal 是事件驱动的，状态一变立刻刷。

## 5.4 打包成 Windows exe

```bash
# 在 Windows 上（或用 wine/虚拟机）
pip install pyinstaller
pyinstaller --onefile --windowed --name AIGateway \
  --add-data "config.toml;." \
  --add-data "src/gateway/templates;src/gateway/templates" \
  src/gateway/gui/app.py
```

**产物**：单个 `AIGateway.exe`（约 80 MB，含 Python 运行时 + Qt）。

**跨平台注意**（必须处理的坑）：
1. **路径分隔符**：全用 `pathlib.Path`，不用字符串拼 `/`
2. **SQLite 文件锁**：Windows 的 `filelock` 行为和 Linux 不同，WAL 模式要显式设
3. **asyncio 事件循环**：Windows 默认是 `ProactorEventLoop`，与 Qt 的 `QEventLoop` 冲突 →
   用 `qasync` 把 asyncio 挂到 Qt 事件循环上
4. **控制台窗口**：`--windowed` 打包后 `print` 无输出，日志必须写文件 + GUI 面板
5. **PyInstaller 与 asyncio**：要 `--hidden-import` 补 uvicorn 的动态导入

---

# 六、对已有方案的影响（要改哪些）

| 文档 | 影响 |
|---|---|
| `AI网关-最终方案.md` | **大改**：五层 → 五层+GUI层；路由层从两层→四层寻址；新增思考等级章；平台从 Linux→Windows |
| `docs/架构设计.md` | 技术栈加 PySide6 + qasync；目录结构加 `gui/` |
| `docs/数据模型与配置.md` | 加 `model_family` / `provider_route` / `thinking_map`；配置加 `[fallback]` / `[thinking]` |
| `docs/落地总表.md` | **新增 3 批**：G7.5 思考等级、G11 GUI、G12 Windows 打包 |
| `docs/测试策略.md` | 加思考映射测试（每协议 5 档）+ GUI 冒烟测试 |
| `docs/借鉴分析.md` | 不变（借鉴点与 GUI 无关） |

---

# 七、待 Master 拍板

| # | 问题 | 我的推荐 |
|---|---|---|
| **1** | GUI 技术栈用 **PySide6**？还是你更想要 Tauri/Web 那种好看的？（代价：后端要改成子进程） | **PySide6**（后端零重写） |
| **2** | 统一思考档位定 **5 档**（off/low/medium/high/max）够不够？还是你要 3 档（简单）或 7 档（更细） | **5 档** |
| **3** | 型号降级链要**自动推断**（按 generation 数字）还是**手工配置**？ | **手工配**（自动猜会出错，如 `gpt-4o-mini` 和 `gpt-4.1` 谁更弱不好判断） |
| **4** | 默认切换顺序：**质量优先**（先换号换家、最后降型号）还是**省钱优先** | **质量优先** |
| **5** | Windows 版本要求（Win10 / Win11 / 都要）？要不要同时保留 Linux 版 | 都是；**保留 Linux**（代码不变，只多一个打包目标） |

---

*建立时间：2026-11-11 ｜ 各家思考参数来自官方文档实测，非记忆*
