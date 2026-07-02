from __future__ import annotations

import os
from typing import Any, Dict, Optional

import httpx


class CmpfGateway:
    """Gateway for CMPF business APIs.

    The first phase supports a mock mode for local agent development and an
    HTTP mode that calls the existing CMPF carbon API.
    """

    def __init__(
        self,
        mode: Optional[str] = None,
        carbon_api_base_url: Optional[str] = None,
        user_api_base_url: Optional[str] = None,
        auth_token: Optional[str] = None,
        http_client: Optional[Any] = None,
    ) -> None:
        self.mode = mode or os.getenv("CMPF_GATEWAY_MODE", "mock")
        self.carbon_api_base_url = (
            carbon_api_base_url
            or os.getenv("CMPF_CARBON_API_BASE_URL")
            or "http://localhost:80"
        ).rstrip("/")
        self.user_api_base_url = (
            user_api_base_url
            or os.getenv("CMPF_USER_API_BASE_URL")
            or "http://localhost:8083"
        ).rstrip("/")
        self.auth_token = auth_token or os.getenv("CMPF_AUTH_TOKEN")
        self.http_client = http_client or httpx.Client(timeout=10.0)

    def get_dashboard_summary(self, company_id: str, year: int) -> Dict[str, Any]:
        if self.mode == "mock":
            return self._mock_dashboard_summary(company_id=company_id, year=year)

        params = self._year_params(company_id=company_id, year=year)
        return self._get_carbon_api("/dashBoard/scope_total_emission_volume", params=params)

    def get_scope_breakdown(self, company_id: str, year: int) -> Dict[str, Any]:
        if self.mode == "mock":
            return self._mock_scope_breakdown(company_id=company_id, year=year)

        params = self._period_params(company_id=company_id, year=year)
        return self._get_carbon_api("/dashBoard/scope_emission_volume", params=params)
    
    def get_company_info(self, company_id: str) -> Dict[str, Any]:
        if self.mode == "mock":
            return self._mock_get_company_info(company_id=company_id)

        params = self._company_params(company_id=company_id)
        return self._get_user_api("/user/company/searchCompanyList", params=params)

    def _get_carbon_api(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        response = self.http_client.get(
            f"{self.carbon_api_base_url}{path}",
            params=params,
            headers=self._headers(),
        )
        response.raise_for_status()
        return response.json()

    def _headers(self) -> Dict[str, str]:
        headers = {
            "lang": os.getenv("CMPF_LANG", "0"),
            "site-name": os.getenv("CMPF_SITE_NAME", ""),
        }
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        return headers

    def _year_params(self, company_id: str, year: int) -> Dict[str, Any]:
        return {
            "companyId": company_id,
            "year": str(year),
            "startMonth": f"{year}-01",
            "endMonth": f"{year}-12",
            "scopeFlg": "",
        }

    def _period_params(self, company_id: str, year: int) -> Dict[str, Any]:
        return {
            "companyId": company_id,
            "startMonth": f"{year}-01",
            "endMonth": f"{year}-12",
            "scopeFlg": "",
        }

    def _company_params(self, company_id: str) -> Dict[str, Any]:
        return {
            "companyId": company_id,
        }
    
    def _mock_dashboard_summary(self, company_id: str, year: int) -> Dict[str, Any]:
        scope1 = 1250.4
        scope2 = 2860.8
        scope3 = 9320.6
        total = round(scope1 + scope2 + scope3, 1)
        return {
            "source": "mock",
            "company_id": company_id,
            "year": year,
            "scope1_tco2e": scope1,
            "scope2_tco2e": scope2,
            "scope3_tco2e": scope3,
            "total_tco2e": total,
            "note": "Mock data for LangGraph agent development.",
        }

    def _mock_scope_breakdown(self, company_id: str, year: int) -> Dict[str, Any]:
        return {
            "source": "mock",
            "company_id": company_id,
            "year": year,
            "scopes": [
                {"scope": "Scope1", "emission_tco2e": 1250.4, "share": "9.3%"},
                {"scope": "Scope2", "emission_tco2e": 2860.8, "share": "21.3%"},
                {"scope": "Scope3", "emission_tco2e": 9320.6, "share": "69.4%"},
            ],
        }
    def _mock_get_company_info(self, company_id: str) -> Dict[str, Any]:
        return {
            "source": "mock",
            "company_id": company_id,
            "company_info": [
                {"company_name": "Mock Company",
            "company_address": "123 Main St, Anytown, USA",
            "company_phone": "123-456-7890",
            "company_email": "info@mockcompany.com"},
            ],
        }
