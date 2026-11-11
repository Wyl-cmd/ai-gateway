# Windows 桌面版 + 多层路由方案

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
| ⑤ | 模型有不同**思考深度/方式** | ❌ **不做** | Master 最终裁决：思考强度不归网关管，交给各家 agent 自己做 |

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
│      ↓                                                       │
└─────────────────────────────────────────────────────────────┘
```

**思考强度不在这个拓扑里** —— 网关不解析、不转换、不降级（见 §三）。

**关键设计**：
- **`model_family` 是「能力标识」**，不是「API 里的 model 字符串」。
  一个 family 在不同 provider 下可能有不同的实际 model 名（Azure 的 `deployment-name`、
  中转的别名）。
- **provider 带「能力差异声明」**：同一个 gpt-4o，官方支持 128k 上下文，
  某中转可能只给 32k，或者不支持某个可选参数。

## 2.3 数据模型（新增 `model_family` 与 `provider_capability`）

```sql
-- 模型型号（能力标识，用户面向的）
CREATE TABLE IF NOT EXISTS model_family (
    family          TEXT PRIMARY KEY,      -- "gpt-4o" / "deepseek-chat"
    display_name    TEXT NOT NULL,
    tier            TEXT,                  -- flagship / standard / lite
    generation      INTEGER,               -- 第几代（同家族降级用：5 → 4 → 3.5）
    context_window  INTEGER,               -- 声明值（provider 可能更小，取 min）
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

# 三、⑤ 思考强度：**网关完全不做**（最终裁决）

> **2026-11-11 第三次也是最终裁决**：Master 明确「模型思考深度这个东西让各家 agent 自己去做好吧」，
> 随后进一步划定边界：「AI 网关接口不需要负责这部分，他只需要破甲，还有聚合以及其他那几样就好，
> 思考强度这些东西本身就不归他管」。

## 3.1 网关的职责边界（写死）

```
① 破甲    模型拒答时换技术重试
② 聚合    多供应商 / 多型号 / 多账号，统一一个入口
③ 路由    三层寻址 + 三维降级（账号 / 供应商 / 型号）
④ 说清    每次响应告诉你用了谁（模式 + requested + actual + 为什么）
```

**思考强度不在这四条里。** 网关对它：**不解析、不翻译、不校验、不设约束、不报告**。

## 3.2 为什么这样划（三轮讨论的结论）

| 轮次 | 我提的方案 | 问题 |
|---|---|---|
| 一 | 网关按 `purpose` 猜档位（code_gen→medium） | **外行替内行决策**。不同 agent 适配不同模型，各自最清楚这一步要不要深想 |
| 二 | 纯透传，网关完全不碰 | 我担心「跨协议时参数被静默丢弃」 |
| 三 | 协议亲和路由（auto 不跨协议）+ 翻译报告 | **过度设计**。这是把 agent 的责任揽到网关身上 |
| **最终** | **网关完全不做** | Master 裁决 |

**最终理由**：**思考强度是 agent 与上游之间的协议细节，不是网关的职责。**
网关既不知道 agent 为什么设这个值，也不该替它保证。**agent 要保证自己的参数有效，
正确做法是锁定能听懂该参数的模型（manual 模式），而不是指望网关替它把关。**

## 3.3 连带的两个删除

### 删掉「协议亲和约束」

原本我设计了「auto 路由时把候选集限制在与接入协议相同的上游」—— **删除**。
理由：那仍然是在「替 agent 保证思考参数有效」，属于把别人的责任揽过来。

**后果（如实写进文档，不隐瞒）**：

```
agent 用 Claude 格式发请求（含 thinking.budget_tokens）
     ↓ auto 路由跨协议选到 OpenAI 的 gpt-4o
thinking.budget_tokens 被 OpenAI 忽略
     ↓
agent 以为在用高档思考，实际是默认档
```

**这是 agent 侧的责任**。文档里明确写：**要保证思考参数生效，请用 manual 模式锁定模型**。
网关通过响应头 `X-Gateway-Route-Actual` 告诉它实际用了谁，剩下的判断交给 agent。

### 删掉「思考降级」维度

三维降级（账号 / 供应商 / 型号 / 思考）→ **三维**（账号 / 供应商 / 型号）。

**理由**：降级思考档 = 网关替 agent 决定「少想一点」，属于同一类越界。

## 3.4 保留的一句话提醒（放 README 和 GUI 里）

> **网关不处理思考强度。** 你在请求里带的 `reasoning_effort` / `thinking.budget_tokens` /
> `thinkingConfig` 等参数会**原样透传**给上游。要保证它们生效，请用 **manual 模式锁定模型**；
> **auto 模式可能把你路由到不支持该参数的模型上**，此时参数会被上游忽略（网关不报错也不拦截）。

**这一句是诚实交代，不是功能。** 用户知道边界在哪，比网关替它瞎猜要强。


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

**约 600~800 行 Python**。

> **不含 RelayKit 的 `relayconvert/reasoning/`（Go 2,845 行）。** 那一整块做的是
> 思考等级的跨协议翻译（Intent 对象、七档映射、模型名后缀、诊断码），**网关不做**（见 §三）。
> 这是本项目最大的一笔减法。

**转换时遇到不认识的字段（含思考参数）→ 原样透传给上游，不解析不改写。**
上游认就生效，不认就忽略 —— **网关不代它判断，也不报告**（这是 §3.3 的边界）。

**这是本项目最大的一块工作量** —— 比破甲层（1,981 行）还大。

# 四、② 降级策略：**型号亲和为默认，跨型号需显式开**

> **Master 的设计（2026-11-11）**：
> 「非 auto 模式下选用了某款模型的话，应该是同一个型号的模型可以相互之间进行降级。
> 另外在后台设置可以降级到其他的不同型号的模型，但如果没有设置的话，
> 默认情况下应当是降级到同一个型号的、只是不同提供商的同一型号模型。
> 因为这样的话，像是思考深度或者是其他的参数其实是对应的，只是提供商不同而已。」

## 4.1 这条设计为什么关键：它替代了「参数翻译」

```
同一型号的不同提供商
    ↓
参数语义完全一致（都是 gpt-4o，reasoning_effort 都叫 reasoning_effort；
                  都是 claude-opus-4，thinking.budget_tokens 语义相同）
    ↓
降级到同型号的另一家 → agent 的参数永远有效
    ↓
网关不需要做任何参数解析、翻译、校验
```

**「型号亲和」免费解决了「参数有效性」问题** —— 不需要翻译，只要降级时别跨型号。
这比「协议亲和 + 参数翻译 + 诊断报告」干净得多：网关什么都不用碰。

**跨型号则相反**：`gpt-4o` 的 `reasoning_effort` 和 `claude-opus-4` 的 `budget_tokens`
是完全不同的东西，没有任何可靠的自动换算。所以**跨型号必须显式开启，且开启后网关只做透传**。

## 4.2 三个降级维度（层级关系）

```
第一层  账号轮巡      同 provider 换 key            ┃ 永远开
        openai:key1 → openai:key2                ┃
                                                  ┃
第二层  供应商切换    同型号、换一家                ┃ ★ 默认开（这是主力）
        openai-official/gpt-4o                  ┃
          → azure/gpt-4o → relay-a/gpt-4o       ┃
                                                  ┃
第三层  型号降级      换成别的型号                  ┃ ✕ 默认关，需显式开
        gpt-4o → gpt-4o-mini → claude-haiku     ┃
```

**默认只走前两层。** 第三层关掉时，同型号所有家都挂了 → **直接失败**，不偷偷换型号。

## 4.3 配置

```toml
[fallback]
# 默认顺序：先换号，再换同型号的家。型号降级不在默认顺序里。
order = ["account", "provider"]

# ── 第一层：账号轮巡（永远开，不可关）──
[fallback.account]
max_attempts = 3              # 最多试 3 个 key

# ── 第二层：供应商切换（同型号，默认开）──
[fallback.provider]
enabled         = true
same_family_only = true       # ★ 核心：只换同型号的不同供应商
max_attempts    = 3
# 同型号内的供应商优先级（不写则按 provider_route.priority 字段）
prefer = ["openai-official", "azure", "relay-a"]

# ── 第三层：型号降级（默认关）──
[fallback.model]
enabled = false               # ★ 默认不跨型号
# 只有 enabled=true 时才用下面这些
chain = []                    # 显式降级链，留空则不降
require_same_protocol = true  # 开跨型号也建议只跳同协议族
```

## 4.4 三种配置的语义（写在 GUI 预设里）

### 预设甲：**保守（默认）** —— 绝不换型号

```toml
order = ["account", "provider"]
[fallback.provider]
same_family_only = true
[fallback.model]
enabled = false
```

**行为**：`gpt-4o` 的所有供应商都挂了 → **返回 503，告诉你每一家为什么挂**，不偷偷给 `gpt-4o-mini`。
**理由**：你要的是 `gpt-4o`。偷偷降级到 mini，输出质量变了你还以为是 4o 给的 —— 这就是骗人。

### 预设乙：**同协议内降级**（推荐给 agent 用）

```toml
order = ["account", "provider", "model"]
[fallback.model]
enabled = true
chain = ["gpt-4o", "gpt-4o-mini"]     # 同家族内降一代
require_same_protocol = true          # 不跳到 Claude/Gemini
```

**行为**：`gpt-4o` 全挂 → 降到 `gpt-4o-mini`（同协议，参数仍大致可用）→ 仍挂才失败。
**响应里必须标 `route_source=model_downgrade` + `requested≠actual` 告警。**

### 预设丙：**尽力可用**（要开才生效）

```toml
order = ["account", "provider", "model"]
[fallback.model]
enabled = true
require_same_protocol = false         # 允许跨协议
chain = ["gpt-4o", "claude-sonnet-4", "deepseek-chat"]
```

**行为**：可以跳到 Anthropic 协议。**此时 agent 的思考参数会失效**（见 §三），
但网关**不翻译、不报错**，只通过 `X-Gateway-Route-Actual` 告诉它实际用了谁。

## 4.5 型号降级必须显式报「换了型号」

跨型号是**能力变化**，不是「同型号换了个来源」。所以：

```http
X-Gateway-Route-Requested: gpt-4o
X-Gateway-Route-Actual: gpt-4o-mini
X-Gateway-Route-Source: model_downgrade
X-Gateway-Route-Warning: model_changed=gpt-4o->gpt-4o-mini
```

body 的 `x_gateway` 里 `route_mismatch: true` + `model_changed: true`。

**对比「同型号换供应商」则不算 mismatch**：
```http
X-Gateway-Route-Requested: openai-official/gpt-4o
X-Gateway-Route-Actual: azure/gpt-4o
X-Gateway-Route-Source: provider_switch
# 没有 Route-Warning —— 型号没变，能力没变，只是换了来源
```

**为什么这个区别重要**：
- 换供应商 → 你要的东西没变，只是从另一家买（可以不管）
- 换型号 → **你要的东西变了**（必须知道）

## 4.6 GUI 设置界面

```
┌─ 降级策略 ────────────────────────────────────────┐
│                                                   │
│  ☑ 账号轮巡        配额耗尽时换 key   最多 [3] 个  │
│                                                   │
│  ☑ 供应商切换      同型号，换一家     最多 [3] 家  │
│     ★ 只换同型号的供应商（推荐，参数语义一致）      │
│     优先顺序: 官方 → Azure → 中转A  [拖拽调整]     │
│                                                   │
│  ☐ 型号降级        换成别的型号       ← 默认不勾   │
│     降级链: gpt-4o → gpt-4o-mini        [编辑]    │
│     ☑ 只在同协议族内跳（不跨 Claude/Gemini）       │
│                                                   │
│  预设: [保守(默认)] [同协议内降级] [尽力可用]      │
│                                                   │
│  当前策略预览:                                     │
│    gpt-4o(官方) → gpt-4o(Azure) → gpt-4o(中转A)   │
│    → 失败返回 503（不降型号）                       │
└───────────────────────────────────────────────────┘
```

**「当前策略预览」这一栏是关键** —— 用户不用脑补「我这样配会怎么走」，
直接看到完整路径。

## 4.7 与 §三 的关系（两句话讲清）

| | 网关做什么 |
|---|---|
| **思考参数** | 完全不管，原样透传 |
| **降级范围** | 默认限制在**同型号**内 —— 这就是保证参数有效的唯一手段 |

**这样设计的好处**：网关不需要理解任何参数的含义，只需要**限制降级范围**，
就保证了 agent 的参数不会因为降级而失效。**用「拓扑约束」代替「语义翻译」，后者永远做不全。**


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

## 5.2 平台形态：Windows 托盘 / Linux 窗口（Master 裁决）

| 平台 | 形态 | 理由 |
|---|---|---|
| **Windows** | **系统托盘常驻**（`QSystemTrayIcon`），主窗口按需弹出 | 网关是常驻后台服务，托盘最合适：不占任务栏、不打断工作、右键即管理。**Windows 用户习惯托盘** |
| **Linux** | **普通窗口**（`QMainWindow`），可选最小化 | Linux 桌面托盘（StatusNotifier/AppIndicator）依赖桌面环境，GNOME 默认不给托盘、KDE 有但风格不一 —— **塞进去会拥挤且不可靠** |

**平台检测与降级**（不能硬编码）：

```python
import sys
from PySide6.QtWidgets import QSystemTrayIcon

def gui_mode() -> str:
    """决定用托盘还是普通窗口。"""
    if sys.platform != "win32":
        return "window"                       # Linux/macOS 一律窗口
    if not QSystemTrayIcon.isSystemTrayAvailable():
        return "window"                       # Windows 上托盘不可用（极罕见）也别崩
    return "tray"
```

**托盘行为（Windows）**：

| 操作 | 行为 |
|---|---|
| 关闭主窗口 | **最小化到托盘**，不退出（首次弹一次气泡说明「已最小化到托盘」） |
| 单击托盘图标 | 显示/隐藏主窗口 |
| 双击托盘图标 | 显示主窗口并聚焦 |
| 右键托盘图标 | 菜单：显示主窗口 / 暂停服务 / 自动·手动切换 / 当前状态 / 退出 |
| 托盘 tooltip | `AI Gateway · AUTO · deepseek-chat · 运行中` ← **不打开窗口就能看到当前用谁** |
| 托盘图标颜色 | 绿=正常 / 黄=有降级 / 红=全部不可用 / 灰=暂停 |

**tooltip 显示当前模型是关键**（对应 §五 Master 的④要求）——
不用点开窗口，鼠标悬停就知道「现在是 auto 模式、用的 deepseek-chat」。

**Linux 窗口行为**：

| 操作 | 行为 |
|---|---|
| 关闭窗口 | 退出程序（Linux 下关窗就是退出，符合习惯） |
| 最小化 | 正常最小化到任务栏 |
| 启动参数 | `--tray` 可强制尝试托盘（给 KDE 等支持的桌面用），失败自动降级为窗口 |

## 5.3 现代化设计（Master 要求「尽量现代化」）

**用 QSS（Qt Style Sheets）做深色主题**，不引入额外框架：

```css
/* 深色 + 圆角 + 柔和阴影 的核心几条 */
QMainWindow, QDialog { background: #1a1b1e; color: #e4e4e7; }
QCard { background: #25262b; border: 1px solid #2c2e33; border-radius: 8px; }
QStatusDot[state="ok"]   { color: #40c057; }
QStatusDot[state="warn"] { color: #fab005; }
QStatusDot[state="down"] { color: #fa5252; }
QTableWidget { border: none; gridline-color: #2c2e33; alternate-background-color: #1f2023; }
QPushButton { border: 1px solid #373a40; border-radius: 6px; padding: 6px 14px; }
QPushButton:hover { background: #2c2e33; }
```

**现代感的四个具体手法**（都是 Qt 自带能力，不加依赖）：

1. **状态用色点不用文字**：`●正常` / `▲冷却` / `✕无key` —— 扫一眼就懂
2. **卡片式分区**：当前路由 / 降级链 / 路由表各是一张圆角卡片，不用表格线框死
3. **表格隔行淡底 + 无竖线**：只留横向分隔，视觉更轻
4. **等宽字体显示数字**：延迟、token、金额对齐用 `JetBrains Mono` / `Consolas` 回退

**不做的事**（自用工具，避免过度工程）：
- 不做动画转场
- 不做自定义标题栏（跨平台问题多）
- 不引入 QML/Qt Quick（学习成本换不来收益）


## 5.4 实时状态推送

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

## 5.5 打包（Windows exe + Linux 二进制）

```bash
# 在 Windows 上（或用 wine/虚拟机）
pip install pyinstaller
pyinstaller --onefile --windowed --name AIGateway \
  --add-data "config.toml;." \
  --add-data "src/gateway/templates;src/gateway/templates" \
  src/gateway/gui/app.py
```

**产物**：单个 `AIGateway.exe`（约 80 MB，含 Python 运行时 + Qt）。

**Linux 打包**（同样 PyInstaller，一份代码两个目标）：

```bash
pyinstaller --onefile --windowed --name AIGateway \
  --add-data "config.toml:." \
  src/gateway/gui/app.py
# 产物：dist/AIGateway（约 90 MB）
# 注意 Linux 用 ':' 当路径分隔符，Windows 用 ';'
```

**同一个入口文件按平台自动选形态**（§5.2 的 `gui_mode()`），不打两个版本。

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
| `AI网关-最终方案.md` | **大改**：加 GUI 层；路由层从两层→四层寻址；平台从 Linux→Windows |
| `docs/架构设计.md` | 技术栈加 PySide6 + qasync；目录结构加 `gui/` |
| `docs/数据模型与配置.md` | 加 `model_family` / `provider_route`；配置加 `[fallback]` |
| `docs/落地总表.md` | **新增 3 批**：G7.5 协议转换、G11 GUI、G12 Windows 打包 |
| `docs/测试策略.md` | 加协议转换矩阵测试（12 个方向）+ GUI 冒烟测试 |
| `docs/借鉴分析.md` | 不变（借鉴点与 GUI 无关） |

---

# 七、待 Master 拍板

## 已被 Master 裁决解决的（不再是问题）

| 原问题 | 裁决 |
|---|---|
| ~~思考档位定几档~~ | **不做** —— 思考强度整块不归网关（§三） |
| ~~型号降级链自动推断还是手工配~~ | **默认不降型号** —— 手工配是唯一的开启方式（§4.3） |
| ~~默认切换顺序质量优先还是省钱优先~~ | **保守为默认** —— 只换账号 + 同型号换供应商，绝不偷偷换型号（§4.4 预设甲） |

## 剩余两项

| # | 问题 | 我的推荐 |
|---|---|---|
| **1** | GUI 技术栈用 **PySide6**？还是你更想要 Tauri/Web 那种好看的？（代价：Python 后端要改成子进程） | **PySide6** —— 后端零重写，同进程直接调 |
| **2** | Windows 版本要求（Win10 / Win11 / 都要）？要不要同时保留 Linux 版 | 都支持；**保留 Linux**（代码不变，只多一个打包目标） |


*建立时间：2026-11-11 ｜ 协议矩阵与上游清单来自 New API 源码实测，非记忆*
*思考强度整块不做 —— Master 最终裁决，见 §三*
