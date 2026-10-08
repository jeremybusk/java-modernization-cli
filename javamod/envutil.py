"""Shared env-var-as-default helper.

Every tunable in javamod can be set three ways, in increasing priority:
shipped default -> ``JAVAMOD_*`` environment variable -> CLI flag. That lets a
team export a handful of env vars once (target Java, profile, recipe source,
provider) and then run the same bare command against repo after repo with no
per-repo flags and no config file, per the project's "little config as
possible, repeatable" goal.
"""
from __future__ import annotations

import os
from typing import Callable, TypeVar

T = TypeVar("T")


def env_default(name: str, default: T, cast: Callable[[str], T] | None = None) -> T:
    """Return the environment override for *name*, cast like *default*, or *default*."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    if cast is not None:
        return cast(raw)
    if isinstance(default, bool):
        return raw.strip().lower() in {"1", "true", "yes", "on"}  # type: ignore[return-value]
    if isinstance(default, int):
        return int(raw)  # type: ignore[return-value]
    return raw  # type: ignore[return-value]
