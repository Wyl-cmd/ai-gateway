"""AI 路由一站式网关。

层次（每层只做自己的事，见 docs/架构设计.md 层间纪律）：
    ⓪ 模式层  auto / manual  —— 决定谁来选路由
    ① 接入层  OpenAI / Anthropic 协议 —— 协议分流
    ② 破甲层  四级阶梯 —— 模型拒答时换技术重试
    ③ 路由层  四层寻址 + 三层降级 —— 选谁
    ④ 转发层  provider adaptor —— 字段映射
    ⑤ 可观测  route_trace + token 计量 + 成本账本
    ⑥ GUI     Windows 托盘 / Linux 窗口
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
