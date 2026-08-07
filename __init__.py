"""Headless Atrinik gameplay client and automation engine."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .client import AtrinikClient, ClientConfig
    from .model import GameState

__all__ = ["AtrinikClient", "ClientConfig", "GameState"]


def __getattr__(name: str):
    if name in {"AtrinikClient", "ClientConfig"}:
        from .client import AtrinikClient, ClientConfig
        return {"AtrinikClient": AtrinikClient, "ClientConfig": ClientConfig}[name]
    if name == "GameState":
        from .model import GameState
        return GameState
    raise AttributeError(name)
