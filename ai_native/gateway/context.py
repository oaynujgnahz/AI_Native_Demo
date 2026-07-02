from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass(frozen=True)
class BusinessContext:
    user_id: str = "local-user"
    company_id: Optional[str] = None
    permissions: List[str] = field(default_factory=lambda: ["cmpf:read"])
    tenant_id: str = "local"

    def has_permission(self, permission: str) -> bool:
        return permission in self.permissions
