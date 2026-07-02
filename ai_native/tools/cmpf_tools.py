from __future__ import annotations

from typing import Any, Dict

from langchain_core.tools import tool

from ai_native.gateway.cmpf_client import CmpfGateway


def build_cmpf_tools(gateway: CmpfGateway):
    @tool
    def get_emission_dashboard(company_id: str, year: int) -> Dict[str, Any]:
        """Get CMPF dashboard emission summary for a company and year."""
        return gateway.get_dashboard_summary(company_id=company_id, year=year)

    return [get_emission_dashboard]
