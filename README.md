# AI Gateway —— AI 路由一站式网关

> 一个自用的本地 AI 网关：**统一入口 + 自动破甲 + token 压缩 + 智能路由 + 多账号熔断**。
> 借鉴三个开源项目 + KXNS 的破甲层，按「借精华弃糟粕」逐项登记。

## 现在做到哪一步

```
✅ 三个参考项目已下载（refs/，40 M）
✅ 借鉴分析（docs/借鉴分析.md）—— 每个项目成对登记「借什么/弃什么」
✅ 架构设计（docs/架构设计.md）—— 六层架构 + 技术栈决策
⬜ 代码实现 —— 待 Master 拍板三项后开工
```

## 三层要解决的问题

| 问题 | 解法 | 借自 |
|---|---|---|
| 模型动不动拒答（「我不能协助攻击」） | **破甲层**：身份锚定 + 45 条拒绝检测 + reframe retry | KXNS `soul/jailbreak/` |
| prompt 太长烧 token | **压缩层**：caveman / ponytail 策略 + headroom 预算判断 | 9router `open-sse/rtk/` |
| 多账号、多 provider、谁挂了不知道 | **路由层**：健康租约 + 失败分类 + rank_targets | freellmpool `router.py` |
| 各家 API 字段不一样 | **转发层**：统一 adaptor 接口 | one-api `relay/adaptor/` |

## 六层架构

```
⓪ 模式层    AUTO / MANUAL —— 决定「谁来选路由」
① 接入层    OpenAI 兼容 / Anthropic / Responses —— 协议分流
② 破甲层    身份锚定 + 拒绝检测 + reframe retry
③ 压缩层    RTK 策略注册表 + autodetect + headroom
④ 路由层    健康租约 + 失败分类 + rank_targets + 能力表
⑤ 转发层    38 家 provider adaptor
```

**层间纪律**：每层只做自己的事。破甲不改路由、压缩不改语义、路由不改协议、
适配不管健康（失败上抛）。

## 三家都没做的、我们要做的

| 能力 | 价值 |
|---|---|
| **可解释路由 `route_trace`** | 每次返回候选集 + 排除原因 + 决策依据，出问题不用猜 |
| **压缩效果归因** | 9router 说省 20-40% 但没做 A/B。我们要能回答「压完质量掉多少」 |
| **成本预算硬门** | 到线就降级/拒绝，不是超了才告警 |

## 文档

- `docs/借鉴分析.md` —— 三个项目逐个「借什么 / 弃什么」成对登记
- `docs/架构设计.md` —— 六层架构、技术栈理由、目录结构、首版范围

## 参考项目（`refs/`，不入 git）

| 项目 | 上游 | 定位 |
|---|---|---|
| one-api | `songquanpeng/one-api` | API 管理与分发，38 个 provider adaptor |
| freellmpool | `0xzr/freellmpool` | 免费 LLM 池，23 providers / 178 routes |
| 9router | `decolua/9router` | AI 路由 + RTK token 节省 20-40% |

重新下载：

```bash
cd refs
git -c http.proxy= -c https.proxy= clone --depth=1 https://github.com/songquanpeng/one-api.git
git -c http.proxy= -c https.proxy= clone --depth=1 https://github.com/0xzr/freellmpool.git
git -c http.proxy= -c https.proxy= clone --depth=1 https://github.com/decolua/9router.git
```

## 状态

**设计阶段完成，待开工。** 三项待拍板见 `docs/架构设计.md` §十。
