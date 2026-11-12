"""模式层：auto / manual + 三档寻址 + 三种降级策略。"""

from .manual import FallbackPolicy, ManualTarget, TargetLock, parse_target
from .router import ModeRouter, RouteFailed

__all__ = [
    "FallbackPolicy", "ManualTarget", "TargetLock", "parse_target",
    "ModeRouter", "RouteFailed",
]
