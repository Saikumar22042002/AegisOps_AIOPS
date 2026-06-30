"""Helpers for graph nodes to reach per-run runtime (emitter) and shared services."""

from __future__ import annotations

from typing import Any

from .events import Emitter


def emitter_of(config: dict[str, Any]) -> Emitter:
    return config["configurable"]["emitter"]


def conf(config: dict[str, Any], key: str, default: Any = None) -> Any:
    return config.get("configurable", {}).get(key, default)
