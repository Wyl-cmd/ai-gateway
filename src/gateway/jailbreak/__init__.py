"""破甲层：技术基类 + 规则变异 + 模板注入 + 拒绝检测 + 有效性记忆。"""

from .refusal import (
    L1_PHRASES_EN,
    L1_PHRASES_ZH,
    RefusalDetector,
    RefusalVerdict,
    detect,
)
from .technique import (
    TECHNIQUE_REGISTRY,
    Cost,
    Lang,
    Level,
    Technique,
    get_technique,
    list_techniques,
    select_techniques,
)

__all__ = [
    "L1_PHRASES_EN", "L1_PHRASES_ZH", "RefusalDetector", "RefusalVerdict", "detect",
    "TECHNIQUE_REGISTRY", "Cost", "Lang", "Level", "Technique",
    "get_technique", "list_techniques", "select_techniques",
]
