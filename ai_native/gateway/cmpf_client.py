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
        # Production requests pass the user's token at call time. The optional
        # constructor value exists only for isolated client tests.
        self.auth_token = auth_token
        self.http_client = http_client or httpx.Client(timeout=10.0)

    def get_dashboard_summary(
        self,
        company_id: str,
        year: int,
        company_start_month: int = 1,
        auth_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        if self.mode == "mock":
            return self._mock_dashboard_summary(company_id=company_id, year=year)

        params = self._year_params(
            company_id=company_id, year=year, company_start_month=company_start_month
        )
        return self._get_carbon_api(
            "/dashBoard/scope_total_emission_volume",
            params=params,
            auth_token=auth_token,
        )

    def get_scope_breakdown(
        self,
        company_id: str,
        year: int,
        company_start_month: int = 1,
        auth_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        if self.mode == "mock":
            return self._mock_scope_breakdown(company_id=company_id, year=year)

        params = self._period_params(
            company_id=company_id, year=year, company_start_month=company_start_month
        )
        return self._get_carbon_api(
            "/dashBoard/scope_emission_volume",
            params=params,
            auth_token=auth_token,
        )
    
    def get_company_info(
        self,
        company_id: str,
        auth_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        if self.mode == "mock":
            return self._mock_get_company_info(company_id=company_id)

        return self._get_user_api(
            "/user/company/getCompanyInfo",
            params={"companyId": company_id},
            auth_token=auth_token,
        )

    def list_direct_child_companies(
        self, auth_token: Optional[str] = None
    ) -> Dict[str, Any]:
        if self.mode == "mock":
            return {"body": []}
        return self._get_user_api(
            "/user/company/options",
            params={"mode": "01"},
            auth_token=auth_token,
        )

    def get_company_start_months(
        self, auth_token: Optional[str] = None
    ) -> Dict[str, Any]:
        if self.mode == "mock":
            return {"body": {"cmpf-demo": 1}}
        return self._get_user_api(
            "/user/company/getCompanyStartMonth",
            params={"mode": "01"},
            auth_token=auth_token,
        )

    def get_scope_summary(
        self,
        company_id: str,
        year: int,
        company_start_month: int,
        scope: int,
        locale: str,
        auth_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        if self.mode == "mock":
            return {
                "body": [
                    {"largeItem": "Fuel", "emissionVolume": 1250.4},
                    {"largeItem": "Electricity", "emissionVolume": 2860.8},
                    {"largeItem": "Supply chain", "emissionVolume": 9320.6},
                ]
            }
        return self._get_carbon_api(
            "/analysis/scopeSummary",
            params=self._analysis_params(
                company_id, year, company_start_month, locale, scope
            ),
            auth_token=auth_token,
        )

    def get_scope_emission_for_month(
        self,
        company_id: str,
        year: int,
        company_start_month: int,
        scope: Optional[int],
        locale: str,
        auth_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        if self.mode == "mock":
            return {
                "body": [
                    {
                        "activityMonth": f"{year}-{month:02d}",
                        "emissionVolume": round(800 + month * 71.5, 1),
                    }
                    for month in range(1, 13)
                ]
            }
        return self._get_carbon_api(
            "/analysis/scopeEmissionForMonth",
            params=self._analysis_params(
                company_id, year, company_start_month, locale, scope
            ),
            auth_token=auth_token,
        )

    def get_top_activity_items_by_emission(
        self,
        company_id: str,
        year: int,
        company_start_month: int,
        locale: str,
        auth_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        if self.mode == "mock":
            return {
                "body": {
                    "totalEmissionVolume": 10000,
                    "items": [
                        {
                            "emissionSourceName": f"Activity {index}",
                            "emissionVolume": float(1100 - index * 83),
                        }
                        for index in range(1, 11)
                    ],
                }
            }
        return self._get_carbon_api(
            "/analysis/topActivityItemsByEmission",
            params={
                "companyId": company_id,
                "year": year,
                "companyStartMonth": company_start_month,
                "languageKbn": 1 if locale == "en" else 0,
            },
            auth_token=auth_token,
        )

    def list_analysis_bases(
        self,
        company_id: str,
        locale: str,
        auth_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        if self.mode == "mock":
            return {
                "body": [
                    {"baseId": 10, "baseName": "東京拠点"},
                    {"baseId": 20, "baseName": "大阪拠点"},
                ]
            }
        return self._get_carbon_api(
            "/analysis/baseInfoByCompanyGroup",
            params={
                "companyId": company_id,
                "language": "1" if locale == "en" else "0",
            },
            auth_token=auth_token,
        )

    def get_base_type_emission(
        self, payload: Dict[str, Any], auth_token: Optional[str] = None
    ) -> Dict[str, Any]:
        if self.mode == "mock":
            return {
                "body": [
                    {"baseName": "東京拠点", "emissionVolume": 65.0},
                    {"baseName": "大阪拠点", "emissionVolume": 35.0},
                ]
            }
        return self._post_carbon_api(
            "/analysis/baseTypeEmission", payload, auth_token=auth_token
        )

    def get_base_type_emission_for_month(
        self, payload: Dict[str, Any], auth_token: Optional[str] = None
    ) -> Dict[str, Any]:
        if self.mode == "mock":
            return {
                "body": [
                    {
                        "activityMonth": f"2025{month:02d}",
                        "baseId": 10,
                        "baseName": "東京拠点",
                        "emissionVolume": float(10 + month),
                    }
                    for month in range(1, 13)
                ]
            }
        return self._post_carbon_api(
            "/analysis/baseTypeEmissionForMonth", payload, auth_token=auth_token
        )

    def get_base_large_item_emission(
        self,
        company_id: str,
        base_id: str,
        start_month: str,
        end_month: str,
        auth_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        if self.mode == "mock":
            return {
                "body": [
                    {"largeItem": "燃料", "emissionVolume": 25.0},
                    {"largeItem": "電力", "emissionVolume": 75.0},
                ]
            }
        return self._get_carbon_api(
            "/analysis/baseLargeItemEmission",
            params={
                "companyId": company_id,
                "baseId": base_id,
                "startMonth": start_month,
                "endMonth": end_month,
            },
            auth_token=auth_token,
        )

    def get_base_month_emission(
        self,
        company_id: str,
        base_id: str,
        year: int,
        company_start_month: int,
        auth_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        if self.mode == "mock":
            return {
                "body": [
                    {
                        "activityMonth": f"{year}{month:02d}",
                        "emissionVolume": float(10 + month),
                    }
                    for month in range(1, 13)
                ]
            }
        return self._get_carbon_api(
            "/analysis/baseMonthEmission",
            params={
                "companyId": company_id,
                "baseId": base_id,
                "aimYear": str(year),
                "companyStartMonth": company_start_month,
            },
            auth_token=auth_token,
        )

    def compare_emissions_by_base(
        self, payload: Dict[str, Any], auth_token: Optional[str] = None
    ) -> Dict[str, Any]:
        if self.mode == "mock":
            return {
                "body": [
                    {
                        "baseId": 10,
                        "baseName": "東京拠点",
                        "items": [
                            {"largeItem": "燃料", "emissionVolume": 25.0},
                            {"largeItem": "電力", "emissionVolume": 75.0},
                        ],
                    },
                    {
                        "baseId": 20,
                        "baseName": "大阪拠点",
                        "items": [
                            {"largeItem": "燃料", "emissionVolume": 20.0},
                            {"largeItem": "電力", "emissionVolume": 40.0},
                        ],
                    },
                ]
            }
        return self._post_carbon_api(
            "/analysis/compareByBase", payload, auth_token=auth_token
        )

    def compare_emissions_by_duration(
        self, payload: Dict[str, Any], auth_token: Optional[str] = None
    ) -> Dict[str, Any]:
        if self.mode == "mock":
            return {
                "body": [
                    {
                        "scopeAndTotalData": [
                            {
                                "total": 100.0,
                                "scope1Volume": 30.0,
                                "scope2Volume": 20.0,
                                "scope3Volume": 50.0,
                            }
                        ],
                        "baseTypeData": [],
                        "largeItemData": {"list": [], "total": 0},
                        "monthData": [],
                    },
                    {
                        "scopeAndTotalData": [
                            {
                                "total": 90.0,
                                "scope1Volume": 25.0,
                                "scope2Volume": 20.0,
                                "scope3Volume": 45.0,
                            }
                        ],
                        "baseTypeData": [],
                        "largeItemData": {"list": [], "total": 0},
                        "monthData": [],
                    },
                ]
            }
        return self._post_carbon_api(
            "/analysis/compareByDuration", payload, auth_token=auth_token
        )

    def _get_carbon_api(
        self,
        path: str,
        params: Dict[str, Any],
        auth_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        response = self.http_client.get(
            f"{self.carbon_api_base_url}{path}",
            params=params,
            headers=self._headers(auth_token=auth_token),
        )
        response.raise_for_status()
        return response.json()

    def _post_carbon_api(
        self,
        path: str,
        payload: Dict[str, Any],
        auth_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        response = self.http_client.post(
            f"{self.carbon_api_base_url}{path}",
            json=payload,
            headers=self._headers(auth_token=auth_token),
        )
        response.raise_for_status()
        return response.json()

    def _get_user_api(
        self,
        path: str,
        params: Dict[str, Any],
        auth_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        response = self.http_client.get(
            f"{self.user_api_base_url}{path}",
            params=params,
            headers=self._headers(auth_token=auth_token),
        )
        response.raise_for_status()
        return response.json()

    def _headers(self, auth_token: Optional[str] = None) -> Dict[str, str]:
        headers = {
            "lang": os.getenv("CMPF_LANG", "0"),
            "site-name": os.getenv("CMPF_SITE_NAME", ""),
        }
        token = auth_token or self.auth_token
        if token:
            headers["Authorization"] = token if token.startswith("Bearer ") else f"Bearer {token}"
        return headers

    def _year_params(
        self, company_id: str, year: int, company_start_month: int = 1
    ) -> Dict[str, Any]:
        start, end = self._fiscal_period(year, company_start_month, separator="-")
        return {
            "companyId": company_id,
            "year": str(year),
            "startMonth": start,
            "endMonth": end,
            "scopeFlg": "",
        }

    def _period_params(
        self, company_id: str, year: int, company_start_month: int = 1
    ) -> Dict[str, Any]:
        start, end = self._fiscal_period(year, company_start_month, separator="-")
        return {
            "companyId": company_id,
            "startMonth": start,
            "endMonth": end,
            "scopeFlg": "",
        }

    def _fiscal_period(
        self, year: int, company_start_month: int, separator: str = ""
    ) -> tuple[str, str]:
        start = f"{year}{separator}{company_start_month:02d}"
        if company_start_month == 1:
            end = f"{year}{separator}12"
        else:
            end = f"{year + 1}{separator}{company_start_month - 1:02d}"
        return start, end

    def _analysis_params(
        self,
        company_id: str,
        year: int,
        company_start_month: int,
        locale: str,
        scope: Optional[int],
    ) -> Dict[str, Any]:
        start_month, end_month = self._fiscal_period(year, company_start_month)
        params = {
            "companyId": company_id,
            "year": year,
            "companyStartMonth": company_start_month,
            "startMonth": start_month,
            "endMonth": end_month,
            "clickLevel": 0,
            "language": "1" if locale == "en" else "0",
        }
        if scope is not None:
            params["scope"] = scope
        return params
    
    def _mock_dashboard_summary(self, company_id: str, year: int) -> Dict[str, Any]:
        scope1 = 1250.4
        scope2 = 2860.8
        scope3 = 9320.6
        total = round(scope1 + scope2 + scope3, 1)
        return {
            "source": "mock", "company_id": company_id, "year": year,
            "scope1_tco2e": scope1, "scope2_tco2e": scope2,
            "scope3_tco2e": scope3, "total_tco2e": total,
        }

    def _mock_scope_breakdown(self, company_id: str, year: int) -> Dict[str, Any]:
        return {"body": [
                {"scope": "Scope1", "emission_tco2e": 1250.4, "share": "9.3%"},
                {"scope": "Scope2", "emission_tco2e": 2860.8, "share": "21.3%"},
                {"scope": "Scope3", "emission_tco2e": 9320.6, "share": "69.4%"},
            ]}
    def _mock_get_company_info(self, company_id: str) -> Dict[str, Any]:
        return {"body": {
            "source": "mock", "companyId": company_id, "companyName": "Mock Company",
            "companyAddress": "123 Main St", "companyPhone": "123-456-7890",
            "companyEmail": "info@mockcompany.com",
        }}
