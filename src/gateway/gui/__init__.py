"""GUI 层：平台检测 + 状态（纯逻辑）+ Qt 渲染。"""

from .platform import GUIMode, gui_mode
from .state import (
    AccountRow,
    GatewayState,
    RouteDisplay,
    format_route_line,
    format_short_status,
)

__all__ = [
    "GUIMode", "gui_mode",
    "AccountRow", "GatewayState", "RouteDisplay",
    "format_route_line", "format_short_status",
]
