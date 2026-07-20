from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from ai_native.gateway.auth import Principal


@dataclass(frozen=True)
class RuntimeContext:
    """Request-scoped capabilities that must never enter checkpointed state."""

    principal: Principal
    bearer_token: str = field(repr=False)
    deadline: datetime
    repository: Any
    is_cancelled: Callable[[], bool]
