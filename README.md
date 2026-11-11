# AI Gateway —— AI 路由一站式网关

> 自用的本地 AI 网关：**统一入口 + 自动破甲 + token 压缩 + 智能路由 + 多账号熔断**。
> 借鉴六个开源项目，按「借精华弃糟粕」逐项登记。

## 现在做到哪一步

```
✅ 方案完整（6 份文档 / 2,000+ 行）—— 可直接照表开工
✅ 六个参考项目已下载（refs/，323 M，不入 git）
⬜ 代码实现 —— 待 Master 说「开工」
```

## 要解决的四个问题

| 问题 | 解法 | 借自 |
|---|---|---|
| 模型动不动拒答 | **破甲层**：L0 身份锚定 → L1 规则变形（25 种）→ L2 模板（22 条）→ L3 LLM 重写 → L4 迭代搜索 | KXNS `soul/jailbreak/` + **EasyJailbreak 39 技术 / 237 模板** |
| prompt 太长烧 token | **压缩层**：caveman / ponytail + autodetect + headroom 预算判断 | 9router `open-sse/rtk/` |
| 多账号、谁挂了不知道 | **路由层**：健康租约 + 失败分类（配额耗尽≠服务故障）+ rank_targets | freellmpool `router.py`（1,627 行） |
| 各家 API 字段不一样 | **转发层**：统一 adaptor 接口 | one-api `relay/adaptor/`（38 家） |

## 六层架构

```
⓪ 模式层    AUTO / MANUAL —— 决定「谁来选路由」
① 接入层    OpenAI 兼容 / Anthropic —— 协议分流
② 破甲层    4 级阶梯（L0 身份 → L1 变形 → L2 模板 → L3 重写 → L4 搜索）
③ 压缩层    RTK 策略注册表 + autodetect + headroom
④ 路由层    健康租约 + 失败分类 + rank_targets + 能力表
⑤ 转发层    provider adaptor（首版 5 家）
```

**层间纪律**：破甲不改路由、压缩不改语义、路由不改协议、适配不管健康（失败上抛）。

## 六份文档（照着就能干）

| 文档 | 内容 | 行数 |
|---|---|---|
| `docs/借鉴分析.md` | 六个项目逐个「借什么 / 弃什么」**成对登记** | 144 |
| `docs/架构设计.md` | 技术栈理由 + 六层架构 + 目录结构 + 首版范围 | 358 |
| `docs/破甲增强方案.md` | KXNS 弱在哪 + 39 技术清单 + 4 级阶梯 + 抄/不抄 | 289 |
| **`docs/落地总表.md`** | **施工顺序 G0~G10，每批六要素（改哪个文件/怎么改/测试怎么写/红的判定/验收命令）** | 253 |
| `docs/数据模型与配置.md` | 4 张表 SQL + 完整 TOML 配置 + API 契约 + 错误码 | 267 |
| `docs/测试策略.md` | 破甲怎么测（四层判据）+ 拒绝检测正负样本 + 约 119 条测试规划 | 199 |

## 施工顺序（最短验证路径）

```
G0 骨架 → G1 数据层 → G2 转发层 → G5 压缩层（纯函数最快见效）
  → G4-J4 拒绝检测 → G3 路由层
  → G4-J1/J2/J3 破甲主体 → G6 可观测 → G7 模式层
  → G8 接入层（★ 第一次能真跑通一次请求）
  → G9 CLI → G4-J5/J6/J7 破甲进阶 → G10 端到端
```

**关键里程碑**：G8 完成 = 零件装成机器。

## 三家都没做的、我们要做的

| 能力 | 价值 |
|---|---|
| **可解释路由 `route_trace`** | 每次返回候选集 + 排除原因 + 决策依据，出问题不用猜 |
| **压缩效果归因** | 9router 说省 20-40% 但没做 A/B。我们要能回答「压完质量掉多少」 |
| **技术有效性记忆** | 「模型 × 技术 成功率矩阵」，从「每次从头试」变成「越用越快」 |
| **成本预算硬门** | 到线就降级/拒绝，不是超了才告警 |

## 参考项目（`refs/`，323 M，不入 git）

| 项目 | 体积 | 定位 | 借什么 |
|---|---|---|---|
| one-api | 6.1 M | API 管理分发 | adaptor 契约 / 能力表 |
| freellmpool | 5.7 M | 免费 LLM 池 | Pool 路由核心（1,627 行） |
| 9router | 29 M | token 节省 | RTK 策略注册表 |
| **easyjailbreak** | 11 M | **越狱攻击框架** | **25 规则变异器 + 237 模板** |
| ArrAttack | 468 K | ICLR 2025 通用越狱 prompt | 备选 |
| PyRIT | 271 M | AI 红队编排 | 编排模式（大部分是数据集） |

重新下载：

```bash
cd refs
for r in songquanpeng/one-api 0xzr/freellmpool decolua/9router \
         easyjailbreak/easyjailbreak LLBao/ArrAttack microsoft/PyRIT; do
  git -c http.proxy= -c https.proxy= clone --depth=1 "https://github.com/$r.git"
done
```

## 技术栈

**Python** —— 理由是「能抄多少代码」：freellmpool 的 1,627 行路由 + KXNS 的 981 行破甲
+ EasyJailbreak 的 2,379 行技术库，都能直接搬。

性能不是问题：AI 网关瓶颈在上游 API 延迟（秒级），本地处理是毫秒级。

## 状态

**方案阶段完成**。三处已确认：技术栈 Python / 转发层首版 5 家 / 破甲默认开（起始 L1）。

待办：等 Master 说开工，从 **G0 骨架**开始走四道门。
