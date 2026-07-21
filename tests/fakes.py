from __future__ import annotations


class FakeCmpfGateway:
    """In-memory CMPF stand-in for unit tests. Production code always uses HTTP."""

    mode = "http"

    def list_direct_child_companies(self, auth_token=None):
        return {"body": [{"value": "200", "label": "Child 200"}]}

    def get_company_start_months(self, auth_token=None):
        return {"body": {"100": 4, "200": 1, "cmpf-demo": 1}}

    def get_company_info(self, company_id, auth_token=None):
        return {
            "body": {
                "companyId": company_id,
                "companyName": f"Company {company_id}",
                "companyAddress": "123 Main St",
                "companyPhone": "123-456-7890",
                "companyEmail": "info@example.com",
            }
        }

    def get_dashboard_summary(
        self, company_id, year, company_start_month=1, auth_token=None
    ):
        return {
            "company_id": company_id,
            "year": year,
            "scope1_tco2e": 1250.4,
            "scope2_tco2e": 2860.8,
            "scope3_tco2e": 9320.6,
            "total_tco2e": 13431.8,
            "source": "cmpf",
        }

    def get_scope_breakdown(
        self, company_id, year, company_start_month=1, auth_token=None
    ):
        return {
            "body": [
                {"scope": "Scope1", "emission_tco2e": 1250.4, "share": "9.3%"},
                {"scope": "Scope2", "emission_tco2e": 2860.8, "share": "21.3%"},
                {"scope": "Scope3", "emission_tco2e": 9320.6, "share": "69.4%"},
            ]
        }

    def get_scope_summary(
        self, company_id, year, company_start_month, scope, locale, auth_token=None
    ):
        return {
            "body": [
                {"largeItem": "燃料", "emissionVolume": 12.5},
                {"largeItem": "電力", "emissionVolume": 20.0},
            ]
        }

    def get_scope_emission_for_month(
        self, company_id, year, company_start_month, scope, locale, auth_token=None
    ):
        return {
            "body": [
                {"activityMonth": "2025-04", "emissionVolume": 1.5},
                {"activityMonth": "2025-05", "emissionVolume": 2.5},
            ]
        }

    def get_top_activity_items_by_emission(
        self, company_id, year, company_start_month, locale, auth_token=None
    ):
        return {
            "body": {
                "items": [
                    {"emissionSourceName": "Gas", "emissionVolume": 50.0},
                    {"emissionSourceName": "Power", "emissionVolume": 40.0},
                ]
            }
        }

    def list_analysis_bases(self, company_id, locale, auth_token=None):
        return {
            "body": [
                {"baseId": 10, "baseName": "東京拠点"},
                {"baseId": 20, "baseName": "大阪拠点"},
            ]
        }

    def get_base_type_emission(self, payload, auth_token=None):
        return {
            "body": [
                {"baseName": "東京拠点", "emissionVolume": 65.0},
                {"baseName": "大阪拠点", "emissionVolume": 35.0},
            ]
        }

    def get_base_type_emission_for_month(self, payload, auth_token=None):
        return {
            "body": [
                {
                    "activityMonth": "202504",
                    "baseId": 10,
                    "baseName": "東京拠点",
                    "emissionVolume": 11.0,
                }
            ]
        }

    def get_base_large_item_emission(
        self, company_id, base_id, start_month, end_month, auth_token=None
    ):
        return {
            "body": [
                {"largeItem": "燃料", "emissionVolume": 25.0},
                {"largeItem": "電力", "emissionVolume": 75.0},
            ]
        }

    def get_base_month_emission(
        self, company_id, base_id, year, company_start_month, auth_token=None
    ):
        return {
            "body": [
                {"activityMonth": f"{year}04", "emissionVolume": 11.0},
                {"activityMonth": f"{year}05", "emissionVolume": 12.0},
            ]
        }

    def compare_emissions_by_base(self, payload, auth_token=None):
        return {
            "body": [
                {
                    "baseId": 10,
                    "baseName": "東京拠点",
                    "emissionTotal": 100.0,
                    "scope1Emission": 30.0,
                    "scope2Emission": 20.0,
                    "scope3Emission": 50.0,
                },
                {
                    "baseId": 20,
                    "baseName": "大阪拠点",
                    "emissionTotal": 60.0,
                    "scope1Emission": 20.0,
                    "scope2Emission": 15.0,
                    "scope3Emission": 25.0,
                },
            ]
        }

    def compare_emissions_by_duration(self, payload, auth_token=None):
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
