from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional


@dataclass(frozen=True)
class AnalysisBase:
    base_id: str
    name: str


class BaseResolutionError(ValueError):
    def __init__(
        self, code: str, candidates: Iterable[AnalysisBase] = ()
    ) -> None:
        super().__init__(code)
        self.code = code
        self.candidates = list(candidates)[:20]


class AnalysisBaseResolver:
    def resolve(
        self,
        payload: Any,
        *,
        company_id: Optional[Any] = None,
        base_id: Optional[Any] = None,
        base_name: Optional[Any] = None,
    ) -> AnalysisBase:
        bases = self._parse(payload, company_id=company_id)
        if base_id is not None:
            matches = [item for item in bases if item.base_id == str(base_id)]
        elif base_name is not None and str(base_name).strip():
            needle = str(base_name).strip().casefold()
            matches = [
                item for item in bases if item.name.strip().casefold() == needle
            ]
        else:
            raise BaseResolutionError("base_required", bases)

        if not matches:
            raise BaseResolutionError("base_not_found", bases)
        if len(matches) > 1:
            raise BaseResolutionError("base_ambiguous", matches)
        return matches[0]

    def list(
        self, payload: Any, *, company_id: Optional[Any] = None
    ) -> list[AnalysisBase]:
        return self._parse(payload, company_id=company_id)[:20]

    def _parse(
        self, payload: Any, *, company_id: Optional[Any] = None
    ) -> list[AnalysisBase]:
        body = payload.get("body") if isinstance(payload, dict) else payload
        if not isinstance(body, list):
            return []

        bases: list[AnalysisBase] = []
        for row in body:
            if not isinstance(row, dict):
                continue
            raw_id = row.get("baseId", row.get("id"))
            raw_name = row.get("baseName", row.get("name", row.get("label")))
            row_company_id = row.get("companyId", row.get("company_id"))
            if (
                company_id is not None
                and row_company_id is not None
                and str(row_company_id) != str(company_id)
            ):
                continue
            if raw_id is None or raw_name is None or not str(raw_name).strip():
                continue
            bases.append(
                AnalysisBase(base_id=str(raw_id), name=str(raw_name).strip())
            )
        return bases
