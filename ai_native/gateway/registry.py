from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from ai_native.gateway.audit import JsonlAuditLogger
from ai_native.gateway.cmpf_client import CmpfGateway
from ai_native.gateway.context import BusinessContext

logger = logging.getLogger(__name__)


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

    def openai_tools(self) -> List[Dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": self._parameters_for(tool.name),
                },
            }
            for tool in self.list_tools()
        ]

    def execute(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        context: BusinessContext,
    ) -> ToolExecutionResult:
        tool = self._tools.get(tool_name)
        if tool is None:
            logger.warning("Tool denied: unknown tool=%s arguments=%s", tool_name, arguments)
            return self._deny(tool_name, arguments, context, "unknown_tool")

        if not context.has_permission(tool.required_permission):
            logger.warning(
                "Tool denied: tool=%s required_permission=%s user_id=%s permissions=%s",
                tool_name,
                tool.required_permission,
                context.user_id,
                context.permissions,
            )
            return self._deny(tool_name, arguments, context, "permission_denied")

        logger.info(
            "Tool executing: tool=%s user_id=%s company_id=%s arguments=%s",
            tool_name,
            context.user_id,
            context.company_id,
            arguments,
        )
        data = tool.handler(**arguments, auth_token=context.auth_token)
        self.audit_logger.write(
            tool_name=tool_name,
            status="success",
            user_id=context.user_id,
            tenant_id=context.tenant_id,
            company_id=context.company_id,
            arguments=arguments,
        )
        logger.info("Tool success: tool=%s result_keys=%s", tool_name, list(data.keys()))
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
                description=(
                    "Get annual total carbon emission summary from CMPF dashboard. "
                    "Use this for questions about carbon emissions, total emissions, "
                    "Scope1/2/3 totals, or yearly emission status."
                ),
                required_permission="cmpf:read",
                handler=self.gateway.get_dashboard_summary,
            ),
            "get_scope_breakdown": ToolSpec(
                name="get_scope_breakdown",
                description=(
                    "Get annual Scope1/2/3 emission breakdown from CMPF dashboard. "
                    "Use this for Scope details, category details, breakdown, or 内訳/明细 questions."
                ),
                required_permission="cmpf:read",
                handler=self.gateway.get_scope_breakdown,
            ),
            "get_company_info": ToolSpec(
                name="get_company_info",
                description=(
                    "Get company profile information from CMPF, such as company name, "
                    "address, phone, or email. Do not use for carbon emission data."
                ),
                required_permission="cmpf:read",
                handler=self.gateway.get_company_info,
            ),
        }

    def _parameters_for(self, tool_name: str) -> Dict[str, Any]:
        if tool_name == "get_company_info":
            return {
                "type": "object",
                "properties": {
                    "company_id": {
                        "type": "string",
                        "description": "CMPF company id.",
                    }
                },
                "required": ["company_id"],
                "additionalProperties": False,
            }
        return {
            "type": "object",
            "properties": {
                "company_id": {
                    "type": "string",
                    "description": "CMPF company id.",
                },
                "year": {
                    "type": "integer",
                    "description": "Target fiscal year.",
                },
            },
            "required": ["company_id", "year"],
            "additionalProperties": False,
        }
