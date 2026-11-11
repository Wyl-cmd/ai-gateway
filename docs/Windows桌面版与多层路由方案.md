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

# 四、② 三维切换策略（GUI 可配）

## 4.1 三个独立的切换维度

| 维度 | 触发 | 动作 | 例子 |
|---|---|---|---|
| **A 账号轮巡** | 配额耗尽 / 限流 / 该 key 冷却 | 同 provider 换 key | `openai:key1` → `openai:key2` |
| **B 供应商切换** | 整个 provider 不可用 | 同 family 换 provider | `openai-official` → `azure` → `relay-a` |
| **C 型号降级** | 同 family 所有 provider 都挂 | 换成更低一代/更小的型号 | `gpt-5` → `gpt-4o` → `gpt-4o-mini` |

## 4.2 关键：**顺序可配**（这是 Master ② 的核心要求）

```toml
[fallback]
# 三个维度的尝试顺序（从左到右）
order = ["account", "provider", "model"]
#            ↑ A          ↑ B         ↑ C

# 每一维的开关
[fallback.account]
enabled = true
max_attempts = 3          # 最多换 3 个 key

[fallback.provider]
enabled = true
max_attempts = 2          # 最多换 2 家

[fallback.model]
enabled = true
max_attempts = 2
# 型号降级链（显式指定，不靠"自动猜哪代低"）
chain = ["gpt-5", "gpt-4o", "gpt-4o-mini"]
```

**两种典型配置**：

```toml
# 配置甲：质量优先（先换账号/家，最后才降能力）
order = ["account", "provider", "model"]

# 配置乙：便宜优先（先换便宜供应商，再降型号）
order = ["provider", "account", "model"]
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
     ☑ 账号轮巡  ☑ 供应商切换  ☑ 型号降级

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
│ │ 延迟      320 ms          │ 本次花费 $0.0004                    ││
│ ├──────────────────────────┴────────────────────────────────────┤│
│ │ 降级链（手动模式下"会降到哪"也在这里显示）                        ││
│ │   deepseek-chat(官方,key1) → deepseek-chat(官方,key2)          ││
│ │   → deepseek-chat(中转A) → deepseek-flash(官方) → 无可用        ││
│ └───────────────────────────────────────────────────────────────┘│
├──────────────────────────────────────────────────────────────────┤
│ 路由表                                        [筛选: 全部型号 ▾]  │
│ 型号            供应商   账号  健康  配额   延迟  操作           │
│ deepseek-chat   官方     key1  ●正常  ∞     320ms [停用]        │
│ deepseek-chat   官方     key2  ●正常  ∞     350ms [停用]        │
│ deepseek-chat   中转A    key1  ▲冷却  ∞     890ms [启用]        │
│ deepseek-flash  官方     key1  ●正常  ∞     180ms [停用]        │
│ gpt-5           官方     key1  ✕无key —      —    [配置]          │
├──────────────────────────────────────────────────────────────────┤
│ [切换策略] [总预算] [破甲] [请求日志] [设置]                       │
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
| `AI网关-最终方案.md` | **大改**：加 GUI 层；路由层从两层→四层寻址；平台从 Linux→Windows |
| `docs/架构设计.md` | 技术栈加 PySide6 + qasync；目录结构加 `gui/` |
| `docs/数据模型与配置.md` | 加 `model_family` / `provider_route`；配置加 `[fallback]` |
| `docs/落地总表.md` | **新增 3 批**：G7.5 协议转换、G11 GUI、G12 Windows 打包 |
| `docs/测试策略.md` | 加协议转换矩阵测试（12 个方向）+ GUI 冒烟测试 |
| `docs/借鉴分析.md` | 不变（借鉴点与 GUI 无关） |

---

# 七、待 Master 拍板

| # | 问题 | 我的推荐 |
|---|---|---|
| **1** | GUI 技术栈用 **PySide6**？还是你更想要 Tauri/Web 那种好看的？（代价：后端要改成子进程） | **PySide6**（后端零重写） |
| **2** | 型号降级链要**自动推断**（按 generation 数字）还是**手工配置**？ | **手工配**（自动猜会出错，如 `gpt-4o-mini` 和 `gpt-4.1` 谁更弱不好判断） |
| **3** | 默认切换顺序：**质量优先**（先换号换家、最后降型号）还是**省钱优先** | **质量优先** |
| **4** | Windows 版本要求（Win10 / Win11 / 都要）？要不要同时保留 Linux 版 | 都是；**保留 Linux**（代码不变，只多一个打包目标） |

---

*建立时间：2026-11-11 ｜ 协议矩阵与上游清单来自 New API 源码实测，非记忆*
*思考强度整块不做 —— Master 最终裁决，见 §三*
