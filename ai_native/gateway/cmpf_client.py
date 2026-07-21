from __future__ import annotations

import os
import logging
from time import perf_counter
from typing import Any, Dict, Optional

import httpx


logger = logging.getLogger(__name__)


class CmpfGateway:
    """HTTP gateway for CMPF business APIs.

    Always calls real CMPF carbon/user APIs. Mock mode is not supported.
    """

    def __init__(
        self,
        mode: Optional[str] = None,
        carbon_api_base_url: Optional[str] = None,
        user_api_base_url: Optional[str] = None,
        auth_token: Optional[str] = None,
        http_client: Optional[Any] = None,
    ) -> None:
        configured = (mode or os.getenv("CMPF_GATEWAY_MODE") or "http").strip().lower()
        if configured == "mock":
            raise ValueError(
                "CMPF_GATEWAY_MODE=mock is no longer supported; configure real CMPF API URLs"
            )
        self.mode = "http"
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
        return self._get_user_api(
            "/user/company/getCompanyInfo",
            params={"companyId": company_id},
            auth_token=auth_token,
        )

    def list_direct_child_companies(
        self, auth_token: Optional[str] = None
    ) -> Dict[str, Any]:
        return self._get_user_api(
            "/user/company/options",
            params={"mode": "01"},
            auth_token=auth_token,
        )

    def get_company_start_months(
        self, auth_token: Optional[str] = None
    ) -> Dict[str, Any]:
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
        return self._post_carbon_api(
            "/analysis/baseTypeEmission", payload, auth_token=auth_token
        )

    def get_base_type_emission_for_month(
        self, payload: Dict[str, Any], auth_token: Optional[str] = None
    ) -> Dict[str, Any]:
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
        return self._post_carbon_api(
            "/analysis/compareByBase", payload, auth_token=auth_token
        )

    def compare_emissions_by_duration(
        self, payload: Dict[str, Any], auth_token: Optional[str] = None
    ) -> Dict[str, Any]:
        return self._post_carbon_api(
            "/analysis/compareByDuration", payload, auth_token=auth_token
        )

    def _get_carbon_api(
        self,
        path: str,
        params: Dict[str, Any],
        auth_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self._request_json(
            "get",
            self.carbon_api_base_url,
            path,
            params=params,
            auth_token=auth_token,
        )

    def _post_carbon_api(
        self,
        path: str,
        payload: Dict[str, Any],
        auth_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self._request_json(
            "post",
            self.carbon_api_base_url,
            path,
            payload=payload,
            auth_token=auth_token,
        )

    def _get_user_api(
        self,
        path: str,
        params: Dict[str, Any],
        auth_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self._request_json(
            "get",
            self.user_api_base_url,
            path,
            params=params,
            auth_token=auth_token,
        )

    def _request_json(
        self,
        method: str,
        base_url: str,
        path: str,
        *,
        params: Dict[str, Any] | None = None,
        payload: Dict[str, Any] | None = None,
        auth_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        started = perf_counter()
        status: int | None = None
        try:
            request = getattr(self.http_client, method)
            kwargs: dict[str, Any] = {"headers": self._headers(auth_token=auth_token)}
            if params is not None:
                kwargs["params"] = params
            if payload is not None:
                kwargs["json"] = payload
            response = request(f"{base_url}{path}", **kwargs)
            status = int(getattr(response, "status_code", 200))
            response.raise_for_status()
            body = response.json()
            logger.info(
                "CMPF request completed",
                extra={
                    "endpoint": path,
                    "duration": round(perf_counter() - started, 6),
                    "status": status,
                    "count": _result_count(body),
                },
            )
            return body
        except Exception as exc:
            response = getattr(exc, "response", None)
            if response is not None:
                status = getattr(response, "status_code", status)
            logger.warning(
                "CMPF request failed",
                extra={
                    "endpoint": path,
                    "duration": round(perf_counter() - started, 6),
                    "status": status or 0,
                    "error": type(exc).__name__,
                    "count": 0,
                },
            )
            raise

    def _headers(self, auth_token: Optional[str] = None) -> Dict[str, str]:
        headers = {
            "lang": os.getenv("CMPF_LANG", "0"),
            "site-name": os.getenv("CMPF_SITE_NAME", ""),
        }
        token = auth_token or self.auth_token
        if token:
            headers["Authorization"] = (
                token if token.startswith("Bearer ") else f"Bearer {token}"
            )
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


def _result_count(payload: Any) -> int:
    if isinstance(payload, dict):
        body = payload.get("body", payload)
        if isinstance(body, (list, tuple, set, dict)):
            return len(body)
        return 1
    if isinstance(payload, (list, tuple, set)):
        return len(payload)
    return 0
