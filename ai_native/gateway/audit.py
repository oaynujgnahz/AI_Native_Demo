from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional


class JsonlAuditLogger:
    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or os.getenv("CMPF_AGENT_AUDIT_PATH", "logs/tool_audit.jsonl")

    def write(
        self,
        *,
        tool_name: str,
        status: str,
        user_id: str,
        tenant_id: str,
        company_id: Optional[str],
        arguments: Dict[str, Any],
        error_code: Optional[str] = None,
    ) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tool_name": tool_name,
            "status": status,
            "user_id": user_id,
            "tenant_id": tenant_id,
            "company_id": company_id,
            "arguments": arguments,
        }
        if error_code:
            entry["error_code"] = error_code
        with open(self.path, "a", encoding="utf-8") as audit_file:
            audit_file.write(json.dumps(entry, ensure_ascii=False) + "\n")
