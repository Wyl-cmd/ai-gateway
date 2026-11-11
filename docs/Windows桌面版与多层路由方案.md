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

# 三、⑤ 思考等级：**翻译，不调配**（按 New API / RelayKit 的做法）

> **2026-11-11 两次修正**：
> 第一版我设计了「网关按 purpose 猜档位」→ Master 指出该让 agent 自己决定。
> 第二版我改成「纯透传」→ **纯透传会静默失效**。
> 第三版看 New API `relaykit/reasoning/`（2,845 行）后才定对：**翻译 + 报告**。

## 3.0 三条边界的最终定义

| 谁 | 做什么 | 不做什么 |
|---|---|---|
| **Agent** | 决定「我要多少思考」（用原生参数，或模型名后缀） | 不用管上游是谁、参数叫什么 |
| **网关** | 把 agent 的表达**翻译**成目标上游能懂的参数；**翻译了什么、丢了什么全部报告** | **不替 agent 决定思考深度**；不按「用途」猜档位 |
| 上游 | 按自己协议的参数执行 | — |

**为什么不能纯透传**：agent 按 Claude 格式发 `thinking: {budget_tokens: 16384}`，
路由到 OpenAI 的 gpt-4o → 该字段被静默忽略 → **agent 以为在用高档思考，实际是默认档**。
既不报错也没提示。**这是网关能犯的静默欺骗之一，必须由网关负责翻译。**

## 3.1 七档统一口径（不是 5 档）

```python
Effort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]
```

这七档**不是我发明的**，是从 New API `reasoning/intent.go` 的常量照抄：

```go
EffortNone / EffortMinimal / EffortLow / EffortMedium / EffortHigh / EffortXHigh / EffortMax
```

`xhigh` 是「比 high 更强但还不到 max」的档，实际有上游能兑现（不是凑数档）。

## 3.2 核心抽象：`Intent` 对象，不是静态映射表

**静态映射表（我第一版的设计）在多跳转换时每跳都丢精度**。New API 用「意图对象 + 双向 apply」：

```
           ┌── FromOpenAIChat()      ─┐
输入（四选一）├── FromOpenAIResponses() ─┤   Intent（跨协议中间表达，带精确 budget 数值）
           ├── FromClaude()          ─┤        │
           └── FromGemini()          ─┘        ├──→ ApplyToOpenAIChat()
                                               ├──→ ApplyToOpenAIResponses()
                                               ├──→ Claude 的 max_tokens/budget 调整
                                               └──→ Gemini 的 thinkingConfig 归一化
```

**`Intent` 要带的不只是「档位名」，还有精确数值**：
- `effort`: 七档之一
- `budget_tokens`: 精确 token 数（Claude/Gemini 用）
- `include_thoughts`: 是否回传思考内容
- `source`: 这个意图从哪个协议解析出来的（诊断用）

**为什么必须带数值**：Claude 的 `budget_tokens: 16384` 转成 OpenAI 的 `effort` 会丢精度，
再转回 Gemini 的 `budget` 就还原不回 16384 了。带原值才能在「圆了一圈」的场景下保真。

## 3.3 agent 的三种表达方式（按优先级）

### ① 模型名后缀（最简，推荐给 agent 用）

**agent 不用记各家参数格式**，直接在模型名后加后缀：

```
gpt-5-high            → gpt-5  + reasoning_effort=high
claude-opus-4-max     → claude + budget_tokens=最大
deepseek-v4-low       → deepseek + effort=low
gemini-3-pro-xhigh    → gemini + thinkingConfig 最高档
```

**各家的后缀集合不同**（这是实测，不是统一列表）：

```go
EffortSuffixes           = ["-max","-xhigh","-high","-medium","-low","-minimal"]
OpenAIEffortSuffixes     = ["-max","-xhigh","-high","-medium","-low","-minimal","-none"]
DeepSeekV4EffortSuffixes = ["-none","-low","-max"]        // DeepSeek 只有三档
```

**为什么这个设计对 agent 最友好**：agent 只需要会说「high」，不需要知道
OpenAI 叫 `reasoning_effort`、Claude 叫 `budget_tokens`、Qwen 叫 `enable_thinking`。

### ② 原生参数（agent 自己知道协议时用）

```json
// OpenAI 格式
{"model": "gpt-5", "reasoning_effort": "high"}

// Claude 格式
{"model": "claude-opus-4", "thinking": {"type": "enabled", "budget_tokens": 16384}}

// Gemini 格式
{"model": "gemini-3-pro", "generationConfig": {"thinkingConfig": {"thinkingBudget": 24576}}}
```

网关原样解析成 `Intent`。

### ③ 两者都有 → `MergeExplicitAndSuffix`

**冲突规则**（照抄 New API `MergeExplicitAndSuffix`）：显式参数优先，后缀作为补充；
冲突时返回诊断 `explicit_overrides_suffix`，不静默改。

## 3.4 「不同模型分类不一样」怎么处理

**处理方式从「映射表」改为「三个 apply 函数各自适配」**：

| 上游 | 适配逻辑（New API 实测） |
|---|---|
| **OpenAI** | 直接吃 `reasoning_effort`（七档里 `none`/`minimal` 部分模型不支持 → 降级并诊断） |
| **Claude** | `budget_tokens` **超出模型范围自动调小**，返回诊断 `claude_budget_adjusted`；`max_tokens` 不够时自动抬高，返回 `claude_max_tokens_raised` |
| **Gemini** | `thinkingConfig` **按模型归一化**（2.5 用 budget / 3+ 用 level，两套不兼容） |
| **Qwen 等只有开关的** | `enable_thinking: true/false`，`effort` 精细度丢失 → 诊断 `effort_collapsed_to_boolean` |
| **不支持思考的模型** | 忽略该参数 → 诊断 `reasoning_not_supported`，**响应里明确标出，不假装生效** |

**共同点：所有「做不到完全匹配」的地方，都以诊断返回，不静默吞掉。**

## 3.5 七档 × 上游的兑现表（哪些档实际能兑现）

| 上游 | none | minimal | low | medium | high | xhigh | max |
|---|---|---|---|---|---|---|---|
| OpenAI o/GPT-5 | 部分 | ✅ | ✅ | ✅ | ✅ | ❌降 high | ❌降 high |
| Claude ≤4.6 | ✅关 | ✅1024 | ✅1024 | ✅4096 | ✅16384 | ~24576 | ✅32000 |
| Claude 4.7+ | ✅关 | ? | ✅ | ✅ | ✅ | ? | ~ |
| Gemini 2.5 | ✅0 | ✅1024 | ✅1024 | ✅8192 | ✅24576 | ~ | ✅32768 |
| Gemini 3+ | ✅off | ? | ✅ | ✅ | ✅ | ? | ~ |
| Qwen | ✅false | ⚠️true | ⚠️true | ⚠️true | ⚠️true | ⚠️true | ⚠️true |

`~` = 需实测确认（**本文档不假装知道**）；`⚠️` = 退化成二值。

**这张表的意义**：agent 要 `xhigh`，路由到的上游没有这档 → 降级兑现 + 诊断，
**而不是假装给了 xhigh**。

## 3.6 实现范围（首版）

| 项 | 首版做 | 后续 |
|---|---|---|
| `Intent` 对象 + 七档 | ✅ | |
| `From{OpenAI Chat, OpenAI Responses, Claude, Gemini}` | ✅ 四种 | |
| `Apply{OpenAI Chat, OpenAI Responses, Claude, Gemini}` | ✅ 四种 | |
| 模型名后缀解析（支持各家不同后缀集） | ✅ | |
| `MergeExplicitAndSuffix` | ✅ | |
| 诊断码（`claude_budget_adjusted` 等） | ✅ | |
| 多跳路径的 Intent 透传 | ✅（Claude↔Gemini 走两跳） | |

**这就是 `relaykit/reasoning/` 的 Python 重写**（Go 2,845 行含测试，纯实现约 1,800 行）。


# 三·五、接口协议支持矩阵（Master 要求：覆盖市面主流协议）

> 依据：`QuantumNous/new-api`（50k stars）的 `relaykit/` 模块实测 + `relay/channel/` 40 个上游

## 3.5.1 四种**接入/转发协议**（两两可转）

RelayKit 的核心是「四种协议任意两种之间转换」，请求 / 非流式响应 / 流式事件全覆盖：

| 源 \ 目标 | OpenAI Chat | OpenAI Responses | Claude Messages | Gemini |
|---|---:|---:|---:|---:|
| **OpenAI Chat** | — | `good` | `fair` | `fair` |
| **OpenAI Responses** | `good` | — | `fair` | `fair` |
| **Claude Messages** | `fair` | `fair` | — | `discouraged` |
| **Gemini** | `fair` | `fair` | `discouraged` | — |

- `good` —— 两种协议结构接近，字段基本一一对应
- `fair` —— 主要内容都能转，**协议独有的字段以诊断报告**
- `discouraged` —— Claude ↔ Gemini 请求和响应都经 OpenAI Chat 中转**两跳**，丢字段的机会更多

**转换结果必须带三样东西**（照 RelayKit）：
1. `steps` —— 实际经过的每一跳（`["claude→oai_chat", "oai_chat→gemini"]`）
2. `quality` —— 本次转换的质量等级（good/fair/discouraged）
3. `diagnostics` —— 转换过程里的所有改动与丢失（见 3.5.3）

**为什么必须报告 `steps`**：同一对协议可能有直接转换器和两跳路径，
用户看到 `discouraged` 才知道「这次结果丢了字段，别全信」。

## 3.5.2 四十个上游 channel（`relay/channel/`）

New API 支持的上游，按协议族分类：

| 协议族 | 上游 |
|---|---|
| **OpenAI 兼容**（最大类） | openai / deepseek / moonshot / minimax / mistral / cohere / perplexity / openrouter / siliconflow / jina / ai360 / dify / coze / ollama / replicate / submodel |
| **Anthropic** | claude |
| **Gemini** | gemini / vertex |
| **国内云** | ali（阿里通义）/ baidu / baidu_v2 / tencent（混元）/ volcengine（豆包） |
| **国际云** | aws（Bedrock）/ cloudflare / palm |
| **特殊协议** | jimeng（即梦图像）/ codex / sub2api / mokaai / advancedcustom（自定义） |

**我们的首版不做 40 个。** 原因和原方案一致：**协议族只有 4 类**，同一协议族内的
不同上游只是在 base_url、鉴权方式、模型名映射上有差别 —— 这些用配置表达，不需要各写一个适配器。

**首版策略**：
```
转发协议：4 种全支持（OpenAI Chat / OpenAI Responses / Claude / Gemini）
上游接入：先用 8 个覆盖 4 个协议族
  openai        （OpenAI 兼容族基类）
  deepseek      （OpenAI 兼容，国内主力）
  claude        （Anthropic 族）
  gemini        （Gemini 族）
  vertex        （Gemini 族，企业端点）
  aws_bedrock   （Claude/Gemini 混，特殊鉴权）
  ali           （OpenAI 兼容，国内）
  advanced_custom（自定义，配 JSON 路径映射，覆盖所有长尾）
```

**`advanced_custom` 是关键**：一个「用户可配 JSON 路径映射」的通用适配器，
能把剩下 30 多个上游都接进来**而不写代码**。这是 New API 的做法（`relay/channel/advancedcustom/`），直接抄。

## 3.5.3 诊断码清单（转换丢失必须可见）

**照抄 RelayKit 的诊断机制** —— 每一次「改动了用户的数据」都产生一条诊断：

| 诊断码 | 什么时候 |
|---|---|
| `claude_budget_adjusted` | Claude 的 `budget_tokens` 超出模型范围被调整 |
| `claude_max_tokens_raised` | `max_tokens` 不够容纳 budget 被抬高 |
| `effort_collapsed_to_boolean` | 目标只支持开关，七档精细度丢失 |
| `effort_downgraded_unsupported` | 目标没有该档（如 OpenAI 无 `xhigh`）→ 降级兑现 |
| `reasoning_not_supported` | 目标完全不支持思考参数 → 忽略 |
| `field_dropped_on_hop` | 多跳转换中某个独有字段无法表达 |
| `tool_reencoded` | 工具定义按目标协议重新编码（如 Responses 的 custom tool → function tool） |
| `image_url_fetch_required` | 需要宿主下载 URL 才能转多模态 |

**每条诊断带三样**：`code` / `message`（人话）/ `path`（哪个字段）。

**全部塞进响应头一个字段**（简版）+ body（完整版）：
```http
X-Gateway-Conversion: quality=fair,steps=2,diagnostics=claude_budget_adjusted,field_dropped_on_hop
```

## 3.5.4 首版实现范围

| 项 | 首版 | 理由 |
|---|---|---|
| 四种协议解析/生成 | ✅ | Master 明确要主流协议 |
| 四种协议两两转换 | ✅ | 12 个方向（含多跳） |
| 流式事件转换 | ✅ | 不做流式等于不能用 |
| 工具调用转换 | ✅ | agent 场景必备 |
| 多模态（图片/文件） | ⚠️ 部分 | 先支持 base64 data URL，URL 下载后置 |
| Usage 统一 | ✅ | 计费要用 |
| 诊断码 | ✅ | 不报告丢字段等于骗人 |
| 8 个上游 + advanced_custom | ✅ | 覆盖 4 协议族 + 长尾可配 |

**约 1,800~2,200 行 Python**（对应 RelayKit 的 Go 实现 3,600 行左右，去掉 Go 特有样板）。

**这是本项目最大的一块工作量** —— 比破甲层（1,981 行）还大。

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
