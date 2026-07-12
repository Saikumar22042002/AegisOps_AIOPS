"""LLM provider abstraction — real model selection with a clear error on unknown models (U3)."""

from __future__ import annotations

from .base import LLMProvider, UnknownModelError
from .registry import available_models, get_provider

__all__ = ["LLMProvider", "UnknownModelError", "available_models", "get_provider"]
