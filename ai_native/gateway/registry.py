from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from ai_native.gateway.audit import JsonlAuditLogger
from ai_native.gateway.cmpf_client import CmpfGateway
from ai_native.gateway.context import BusinessContext


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    required_permission: str
    handler: Callable[..., Dict[str, Any]]
    read_only: bool = True


@dataclass(frozen=True)
class ToolExecutionResult:
    tool_name: str
    allowed: bool
    data: Optional[Dict[str, Any]] = None
    error_code: Optional[str] = None


class ToolRegistry:
    def __init__(
        self,
        gateway: CmpfGateway,
        audit_logger: Optional[JsonlAuditLogger] = None,
    ) -> None:
        self.gateway = gateway
        self.audit_logger = audit_logger or JsonlAuditLogger()
        self._tools = self._build_tools()

    def list_tools(self) -> List[ToolSpec]:
        return list(self._tools.values())

    def execute(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        context: BusinessContext,
    ) -> ToolExecutionResult:
        tool = self._tools.get(tool_name)
        if tool is None:
            return self._deny(tool_name, arguments, context, "unknown_tool")

        if not context.has_permission(tool.required_permission):
            return self._deny(tool_name, arguments, context, "permission_denied")

        data = tool.handler(**arguments)
        self.audit_logger.write(
            tool_name=tool_name,
            status="success",
            user_id=context.user_id,
            tenant_id=context.tenant_id,
            company_id=context.company_id,
            arguments=arguments,
        )
        return ToolExecutionResult(tool_name=tool_name, allowed=True, data=data)

    def _deny(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        context: BusinessContext,
        error_code: str,
    ) -> ToolExecutionResult:
        self.audit_logger.write(
            tool_name=tool_name,
            status="denied",
            user_id=context.user_id,
            tenant_id=context.tenant_id,
            company_id=context.company_id,
            arguments=arguments,
            error_code=error_code,
        )
        return ToolExecutionResult(tool_name=tool_name, allowed=False, error_code=error_code)

    def _build_tools(self) -> Dict[str, ToolSpec]:
        return {
            "get_emission_dashboard": ToolSpec(
                name="get_emission_dashboard",
                description="Get annual total emission summary from CMPF dashboard.",
                required_permission="cmpf:read",
                handler=self.gateway.get_dashboard_summary,
            ),
            "get_scope_breakdown": ToolSpec(
                name="get_scope_breakdown",
                description="Get annual Scope1/2/3 emission breakdown from CMPF dashboard.",
                required_permission="cmpf:read",
                handler=self.gateway.get_scope_breakdown,
            ),
            "get_company_info": ToolSpec(
                name="get_company_info",
                description="Get company information from CMPF.",
                required_permission="cmpf:read",
                handler=self.gateway.get_company_info,
            ),
        }
