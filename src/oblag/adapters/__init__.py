from __future__ import annotations

import importlib

from oblag.adapters.base import SourceAdapter

_REGISTRY: dict[str, type[SourceAdapter]] = {}

# Adapter modules ship incrementally; each module calls register() at import time.
_BUILTIN_MODULES: list[str] = ["federal_register", "nist_csrc", "regulations_gov"]


def register(cls: type[SourceAdapter]) -> type[SourceAdapter]:
    _REGISTRY[cls.name] = cls
    return cls


def get_adapter(name: str) -> SourceAdapter:
    _load_builtins()
    try:
        return _REGISTRY[name]()
    except KeyError:
        raise KeyError(f"unknown adapter {name!r}; available: {sorted(_REGISTRY)}") from None


def available_adapters() -> list[str]:
    _load_builtins()
    return sorted(_REGISTRY)


def _load_builtins() -> None:
    for mod in _BUILTIN_MODULES:
        importlib.import_module(f"oblag.adapters.{mod}")
