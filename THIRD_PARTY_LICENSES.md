# 第三方组件与许可声明

本项目包含或改编了以下开源项目的内容。按各项目许可证要求在此声明。

**项目自身的许可证是 AGPL-3.0**（见 `LICENSE`）——因为内置了 AGPL-3.0 的
破甲模板，整个项目沿用同一许可证是最简单且无争议的做法。

---

## 一、包含源码/数据到本仓库的项目

### L1B3RT4S — AGPL-3.0

- 仓库：https://github.com/elder-plinius/L1B3RT4S
- 作者：Pliny the Prompter (elder-plinius)
- 许可：GNU Affero General Public License v3.0
- 本项目使用方式：**原样包含** 37 个 `.mkd` 模板文件于
  `src/gateway/jailbreak/data/pliny/`
- 修改：未修改内容。仅从全库排除了两个超长 many-shot 文件
  （`TOKEN80M8.mkd` 22.9 MB / `TOKENADE.mkd` 1.8 MB），因为它们
  占全库 94% 体积且不适用于按厂商匹配的逻辑

**因为包含了这些 AGPL-3.0 内容，本项目整体采用 AGPL-3.0 发布。**

---

## 二、借鉴了设计/算法但未包含源码的项目

以下项目**没有**任何文件进入本仓库，仅在设计与实现思路上参考。
这里列出是对原作者的致谢，不构成许可证义务。

### PyRIT — MIT

- 仓库：https://github.com/Azure/PyRIT
- 作者：Microsoft
- 借鉴：破甲模板的组织方式与模板库结构（`pyrit/datasets/jailbreak/templates/`）

### EasyJailbreak — MIT

- 仓库：https://github.com/EasyJailbreak/EasyJailbreak
- 借鉴：25 种规则变异器的算法思路（Base64 / Morse / Rot13 / Leetspeak /
  Disemvowel / Reverse / BinaryTree / OddEven 等）
- **未抄的部分**：EasyJailbreak 是**评估**框架（衡量模型抗不抗打），
  本项目是**要过**的工具。抄技术不抄流程，也不抄它「全英文语料」的语言假设

### freellmpool — MIT

- 仓库：https://github.com/0xzr/freellmpool
- 借鉴：多账号池路由的核心思路（健康租约、配额耗尽与服务故障的失败分类）

### one-api — MIT

- 仓库：https://github.com/songquanpeng/one-api
- 借鉴：provider adaptor 的统一接口契约

### 9router — MIT

- 仓库：https://github.com/decolua/9router
- 借鉴：SSE 流式响应的分层解析结构

### garak — Apache-2.0

- 仓库：https://github.com/NVIDIA/garak
- 借鉴：32 个判定器的判定思路（本项目只实现拒绝检测，未移植判定器）

### new-api — Apache-2.0

- 仓库：https://github.com/QuantumNous/new-api
- 参考：四协议互转的质量矩阵设计（本项目未实现协议互转，只做同协议内切换）

### ArrAttack — MIT

- 仓库：https://github.com/LLBao/ArrAttack
- 参考：ICLR 2025 通用越狱 prompt 的构造方式（备选，未采用）

### CL4R1T4S — AGPL-3.0

- 仓库：https://github.com/elder-plinius/CL4R1T4S
- 状态：**仅克隆研究，未使用，未包含任何内容**

### jailbreak_llms — MIT

- 仓库：https://github.com/verazuo/jailbreak_llms
- 状态：**仅克隆研究，未使用，未包含任何内容**

---

## 三、运行时依赖

| 包 | 许可 |
|---|---|
| httpx | BSD-3-Clause |
| pydantic / pydantic-settings | MIT |
| fastapi | MIT |
| uvicorn | BSD-3-Clause |
| PySide6（可选，GUI） | LGPL-3.0 |
| qasync（可选，GUI） | BSD-2-Clause |
| pytest（开发） | MIT |

---

## 四、如果 AGPL-3.0 对你不合适

如果你只想用破甲层而不想受 AGPL 约束，可以：

1. 删除 `src/gateway/jailbreak/data/pliny/` 整个目录
2. 把 `src/gateway/jailbreak/pliny.py` 的 `_CANDIDATES` 改成运行时从
   L1B3RT4S 仓库下载
3. 这样本项目剩余部分可以按更宽松的许可证使用

但内置模板是破甲效果的核心 —— 移除后 `pliny_vendor` 技术会失效，
实测破甲效果从「8208 字完整实现」退化到「131 字软化拒绝」。
