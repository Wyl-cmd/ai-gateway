"""数据访问层。

**唯一入口**：所有表操作都从这里导出的 Repo 走，不要在别处拼 SQL。
（同一个映射写多遍必然漂移 —— 这是 KXNS 那边踩过的坑。）
"""

from gateway.storage.repo import (
    AttemptLogRepo,
    BudgetLedgerRepo,
    RequestLogRepo,
    RouteState,
    RouteStateRepo,
    TechniqueRate,
    connect,
    init_db,
)

__all__ = [
    "connect",
    "init_db",
    "RouteState",
    "RouteStateRepo",
    "TechniqueRate",
    "AttemptLogRepo",
    "RequestLogRepo",
    "BudgetLedgerRepo",
]
