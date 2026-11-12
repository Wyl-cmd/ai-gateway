"""Adaptor 注册表 —— 按名字取适配器。

不做路由决策（那是 G3 路由层的事），只负责「谁在哪」。
"""

from __future__ import annotations

from .base import Adaptor


class Aggregator:
    def __init__(self) -> None:
        self._adaptors: dict[str, Adaptor] = {}

    def register(self, name: str, adaptor: Adaptor) -> None:
        self._adaptors[name] = adaptor

    def get(self, name: str) -> Adaptor:
        if name not in self._adaptors:
            raise KeyError(f"未注册的 provider: {name}")
        return self._adaptors[name]

    def names(self) -> list[str]:
        return list(self._adaptors.keys())

    def __contains__(self, name: str) -> bool:
        return name in self._adaptors
