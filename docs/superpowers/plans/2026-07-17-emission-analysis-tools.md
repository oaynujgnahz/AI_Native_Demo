# CMPF Emission Analysis Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add bounded, auditable natural-language analysis loops for CMPF Scope, site (拠点), and period comparison without changing CMPF.

**Architecture:** The LLM selects one business-capability tool and extracts untrusted names and filters. `EnterpriseAgentService` validates company scope, resolves a site name through CMPF, validates the resulting `baseId`, then calls at most one final analysis method. CMPF DTOs are mapped deterministically to safe `ChartSpec` objects and never sent to the model.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic 2, httpx, OpenAI-compatible tool calling, PostgreSQL JSONB audit metadata, unittest, vanilla JavaScript/ECharts demo.

## Global Constraints

- Do not modify CMPF Java, frontend, database, authentication, or authorization code.
- Reuse the caller's Keycloak Bearer Token for CMPF requests; never persist or log it.
- Company scope remains self plus direct subsidiaries; CMPF remains final authorization authority.
- Run at most 3 preparation steps and 0 or 1 final business analysis tool per turn.
- The mandatory self/direct-subsidiary authorization check runs before the loop and does not consume a preparation step.
- Query company info, fiscal start month, and site list at most once each per company per turn.
- Site matching permits trimmed exact and case-insensitive full matches only; never fuzzy-select.
- Permit 2 to 5 sites for comparisons and periods of at most 36 months.
- Charts permit at most 5 series and 100 total points, finite numbers only, and no code/HTML/functions.
- Preserve all unrelated uncommitted changes in both workspaces.

---

### Task 1: Safe grouped-bar chart contract and renderer

**Files:**
- Modify: `ai_native/gateway/charts.py`
- Modify: `ai_native/demo.html`
- Test: `tests/test_enterprise_gateway.py`

**Interfaces:**
- Produces: `ChartSpec.chart_type` accepting `"grouped_bar"`.
- Produces: `safeOption(spec)` rendering every validated series.

- [ ] **Step 1: Write failing tests**

```python
def test_grouped_bar_accepts_multiple_safe_series(self):
    chart = ChartSpec(
        chart_type="grouped_bar",
        title="2024 vs 2025",
        categories=["Scope 1", "Scope 2"],
        series=[
            ChartSeries(name="2024", values=[1.0, 2.0]),
            ChartSeries(name="2025", values=[1.5, 2.5]),
        ],
        source=ChartSource(tool_name="compare_emission_periods_chart",
            company_id="100", company_name="Company 100", period="2024-2025"),
    )
    self.assertEqual(chart.chart_type, "grouped_bar")

def test_chart_rejects_more_than_one_hundred_total_points(self):
    with self.assertRaises(ValueError):
        ChartSpec(
            chart_type="grouped_bar", title="Too many",
            categories=[str(i) for i in range(51)],
            series=[ChartSeries(name="A", values=[1.0] * 51),
                    ChartSeries(name="B", values=[2.0] * 51)],
            source=ChartSource(tool_name="compare_base_emissions_chart",
                company_id="100", company_name="Company 100", period="2025"),
        )
```

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m unittest tests.test_enterprise_gateway.ChartSpecTest -v`

Expected: `grouped_bar` fails Literal validation and the cross-series total limit is absent.

- [ ] **Step 3: Implement the minimal safe contract**

```python
chart_type: Literal["pie", "line", "horizontal_bar", "grouped_bar"]

if sum(len(item.values) for item in self.series) > 100:
    raise ValueError("chart total data points must not exceed 100")
```

In `safeOption`, validate every series, map `grouped_bar` to ECharts `bar`, and keep all option construction hard-coded:

```javascript
const series = spec.series.map(item => ({
  name: String(item.name), values: item.values.map(Number)
}));
if (series.length > 5 ||
    series.reduce((n, item) => n + item.values.length, 0) > 100 ||
    series.some(item => item.values.length !== categories.length ||
      item.values.some(value => !Number.isFinite(value)))) {
  throw new Error('Invalid chart data');
}
```

- [ ] **Step 4: Verify GREEN**

Run: `.venv/bin/python -m unittest tests.test_enterprise_gateway.ChartSpecTest -v`

Run: `awk '/<script>/{flag=1;next}/<\/script>/{flag=0}flag' ai_native/demo.html | node --check`

Expected: all commands exit 0.

- [ ] **Step 5: Commit**

```bash
git add ai_native/gateway/charts.py ai_native/demo.html tests/test_enterprise_gateway.py
git commit -m "feat: support safe grouped emission charts"
```

---

### Task 2: Typed CMPF analysis client methods

**Files:**
- Modify: `ai_native/gateway/cmpf_client.py`
- Test: `tests/test_enterprise_gateway.py`

**Interfaces:**
- Produces: `list_analysis_bases(company_id, locale, auth_token=None) -> dict`.
- Produces: `get_base_type_emission(payload, auth_token=None) -> dict`.
- Produces: `get_base_type_emission_for_month(payload, auth_token=None) -> dict`.
- Produces: `get_base_large_item_emission(company_id, base_id, start_month, end_month, auth_token=None) -> dict`.
- Produces: `get_base_month_emission(company_id, base_id, year, company_start_month, auth_token=None) -> dict`.
- Produces: `compare_emissions_by_base(payload, auth_token=None) -> dict`.
- Produces: `compare_emissions_by_duration(payload, auth_token=None) -> dict`.

- [ ] **Step 1: Write failing HTTP contract tests**

Use a recording client with `get` and `post`, call all seven methods, and assert:

```python
self.assertEqual([(call.method, call.path) for call in calls], [
    ("GET", "/analysis/baseInfoByCompanyGroup"),
    ("POST", "/analysis/baseTypeEmission"),
    ("POST", "/analysis/baseTypeEmissionForMonth"),
    ("GET", "/analysis/baseLargeItemEmission"),
    ("GET", "/analysis/baseMonthEmission"),
    ("POST", "/analysis/compareByBase"),
    ("POST", "/analysis/compareByDuration"),
])
self.assertEqual(calls[0].params, {"companyId": "100", "language": "0"})
self.assertEqual(calls[1].json, {"companyId": "100"})
self.assertEqual(calls[-1].headers["Authorization"], "Bearer token")
```

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m unittest tests.test_enterprise_gateway.CmpfAnalysisContractTest -v`

Expected: the seven methods are missing.

- [ ] **Step 3: Implement fixed-path GET/POST calls**

```python
def _post_carbon_api(self, path, payload, auth_token=None):
    response = self.http_client.post(
        f"{self.carbon_api_base_url}{path}", json=payload,
        headers=self._headers(auth_token=auth_token),
    )
    response.raise_for_status()
    return response.json()
```

Implement each public method with its exact interface and endpoint. Mock mode returns deterministic bases, monthly rows, and two comparison groups.

- [ ] **Step 4: Verify GREEN**

Run: `.venv/bin/python -m unittest tests.test_enterprise_gateway.CmpfAnalysisContractTest tests.test_cmpf_agent -v`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add ai_native/gateway/cmpf_client.py tests/test_enterprise_gateway.py
git commit -m "feat: add typed CMPF analysis client methods"
```

---

### Task 3: Bounded site resolver

**Files:**
- Create: `ai_native/gateway/base_resolver.py`
- Test: `tests/test_enterprise_gateway.py`

**Interfaces:**
- Produces: `AnalysisBase(base_id: str, name: str)`.
- Produces: `BaseResolutionError(code: str, candidates: list[AnalysisBase])`.
- Produces: `AnalysisBaseResolver.resolve(payload, *, base_id=None, base_name=None) -> AnalysisBase`.

- [ ] **Step 1: Write failing resolver tests**

```python
payload = {"body": [
    {"baseId": 10, "baseName": "東京拠点"},
    {"baseId": 20, "baseName": "OSAKA"},
]}
self.assertEqual(resolver.resolve(payload, base_name=" 東京拠点 ").base_id, "10")
self.assertEqual(resolver.resolve(payload, base_name="osaka").base_id, "20")
with self.assertRaisesRegex(BaseResolutionError, "base_not_found"):
    resolver.resolve(payload, base_name="東京")
with self.assertRaisesRegex(BaseResolutionError, "base_not_found"):
    resolver.resolve(payload, base_id="999")
```

Also test duplicate names return `base_ambiguous`, missing input returns `base_required`, and 25 candidates are capped at 20.

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m unittest tests.test_enterprise_gateway.AnalysisBaseResolverTest -v`

Expected: import fails because the resolver file does not exist.

- [ ] **Step 3: Implement deterministic resolution**

```python
@dataclass(frozen=True)
class AnalysisBase:
    base_id: str
    name: str

class BaseResolutionError(ValueError):
    def __init__(self, code, candidates=()):
        super().__init__(code)
        self.code = code
        self.candidates = list(candidates)[:20]

class AnalysisBaseResolver:
    def resolve(self, payload, *, base_id=None, base_name=None):
        bases = self._parse(payload)
        if base_id is not None:
            matches = [item for item in bases if item.base_id == str(base_id)]
        elif base_name and str(base_name).strip():
            needle = str(base_name).strip().casefold()
            matches = [item for item in bases if item.name.strip().casefold() == needle]
        else:
            raise BaseResolutionError("base_required", bases)
        if not matches:
            raise BaseResolutionError("base_not_found", bases)
        if len(matches) > 1:
            raise BaseResolutionError("base_ambiguous", matches)
        return matches[0]
```

Parse IDs only from `baseId`/`id` and names only from `baseName`/`name`/`label`. Ignore malformed rows.

- [ ] **Step 4: Verify GREEN**

Run: `.venv/bin/python -m unittest tests.test_enterprise_gateway.AnalysisBaseResolverTest -v`

Expected: all resolver tests pass.

- [ ] **Step 5: Commit**

```bash
git add ai_native/gateway/base_resolver.py tests/test_enterprise_gateway.py
git commit -m "feat: resolve CMPF analysis sites safely"
```

---

### Task 4: LLM schemas and multilingual routing

**Files:**
- Modify: `ai_native/gateway/service.py`
- Modify: `ai_native/agent/llm.py`
- Test: `tests/test_cmpf_agent.py`
- Test: `tests/test_enterprise_gateway.py`

**Interfaces:**
- Extends: `ENTERPRISE_TOOL_NAMES` to 13 fixed tools.
- Extends: `_tool_schema(name)` with bounded site arrays and validated month strings.

- [ ] **Step 1: Write failing schema and routing tests**

```python
self.assertEqual(len(planner.tool_names), 13)
self.assertIn("get_base_detail_monthly_chart", planner.tool_names)
self.assertIn("compare_base_emissions_chart", planner.tool_names)
self.assertIn("compare_emission_periods_chart", planner.tool_names)
self.assertIn("拠点", system_prompt)
self.assertIn("据点", system_prompt)
self.assertIn("period comparison", system_prompt)
```

Add deterministic fallback assertions for `東京拠点の月別推移`, `比较东京和大阪据点`, and `两个期间比较`.

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m unittest tests.test_cmpf_agent tests.test_enterprise_gateway.EnterpriseApiTest -v`

Expected: tool-count and missing routing assertions fail.

- [ ] **Step 3: Add fixed schemas and prompt rules**

Add the seven approved names and actual bounds:

```python
"base_names": {
    "type": "array",
    "items": {"type": "string", "minLength": 1, "maxLength": 200},
    "minItems": 2,
    "maxItems": 5,
}
```

Month strings use `pattern: r"^20\d{2}(0[1-9]|1[0-2])$"`. The prompt includes Japanese, Chinese, and English examples, but no CMPF response or site list.

- [ ] **Step 4: Verify GREEN**

Run: `.venv/bin/python -m unittest tests.test_cmpf_agent tests.test_enterprise_gateway.EnterpriseApiTest -v`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add ai_native/gateway/service.py ai_native/agent/llm.py tests/test_cmpf_agent.py tests/test_enterprise_gateway.py
git commit -m "feat: expose site and period analysis tools"
```

---

### Task 5: Controlled loop and analysis execution

**Files:**
- Modify: `ai_native/gateway/service.py`
- Modify: `ai_native/api.py`
- Test: `tests/test_enterprise_gateway.py`

**Interfaces:**
- Consumes: Task 2 client methods, Task 3 resolver, Task 4 schemas.
- Produces: four site charts plus site and period comparison charts.
- Produces: stable errors `base_required`, `base_not_found`, `base_ambiguous`, `too_many_bases`, `invalid_period`, `period_too_long`, `tool_loop_limit`.

- [ ] **Step 1: Write failing bounded-loop tests**

For `東京拠点の2025年月別排出量`, assert:

```python
self.assertEqual(gateway.calls, [
    "list_direct_child_companies", "get_company_info",
    "get_company_start_months", "list_analysis_bases",
    "get_base_month_emission:10",
])
self.assertEqual(result.chart.chart_type, "line")
self.assertEqual(result.chart.source.tool_name, "get_base_detail_monthly_chart")
```

Add tests proving unknown, ambiguous, and foreign `baseId` inputs make zero final analysis calls. Add comparison tests for 2-site success, 6-site rejection, reversed periods, 37-month rejection, and zero-valued baseline.

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m unittest tests.test_enterprise_gateway.ControlledAnalysisLoopTest tests.test_enterprise_gateway.AnalysisComparisonTest -v`

Expected: missing loop and tool branches fail.

- [ ] **Step 3: Implement cached preparation context**

```python
@dataclass
class _ExecutionContext:
    company_name: Optional[str] = None
    start_month: Optional[int] = None
    bases_payload: Any = None
    preparation_steps: int = 0

    def step(self):
        self.preparation_steps += 1
        if self.preparation_steps > 3:
            raise RequestValidationError("tool_loop_limit")
```

Cache each preparation response once per turn. Resolve all requested sites from the same bases payload. Convert resolver failures to stable validation errors without exposing raw DTOs.

- [ ] **Step 4: Implement strict period validation**

```python
def _parse_month(value):
    text = str(value or "")
    if not re.fullmatch(r"20\d{2}(0[1-9]|1[0-2])", text):
        raise RequestValidationError("invalid_period")
    return datetime.strptime(text, "%Y%m")

def _period_months(start, end):
    if start > end:
        raise RequestValidationError("invalid_period")
    count = (end.year - start.year) * 12 + end.month - start.month + 1
    if count > 36:
        raise RequestValidationError("period_too_long")
    return count
```

Require 2 to 5 unique resolved sites for comparison and build CMPF payloads only from validated IDs and normalized months.

- [ ] **Step 5: Implement deterministic chart mappings and audits**

Site composition uses `pie`, site monthly uses `line`, and comparisons use `grouped_bar`. Slice rows before constructing the chart. Compute comparison totals/difference from the finite chart values; compute percentage only when baseline is nonzero. Audit each preparation and final step with safe IDs/counts/status, never Token or raw payload.

- [ ] **Step 6: Preserve safe API errors and verify GREEN**

Ensure validation failures remain HTTP 422 with `detail.code` and never generic 502. Run:

```bash
.venv/bin/python -m unittest tests.test_enterprise_gateway.ControlledAnalysisLoopTest tests.test_enterprise_gateway.AnalysisComparisonTest tests.test_enterprise_gateway.EnterpriseApiTest -v
```

Expected: all tests pass; forbidden or ambiguous requests contain no visualization and make no final CMPF analysis call.

- [ ] **Step 7: Commit**

```bash
git add ai_native/gateway/service.py ai_native/api.py tests/test_enterprise_gateway.py
git commit -m "feat: run bounded CMPF analysis loops"
```

---

### Task 6: Documentation and real CMPF acceptance

**Files:**
- Modify: `README.md`
- Test: `tests/test_enterprise_gateway.py`
- Test: `tests/test_cmpf_agent.py`

**Interfaces:**
- Documents: all 13 tools, exact CMPF paths, loop limits, and examples.
- Verifies: real Keycloak → Gateway → CMPF flow without CMPF changes.

- [ ] **Step 1: Write a failing documentation contract**

```python
readme = Path("README.md").read_text(encoding="utf-8")
self.assertIn("compare_emission_periods_chart", readme)
self.assertIn("/analysis/compareByDuration", readme)
self.assertIn("3 preparation", readme)
```

- [ ] **Step 2: Verify RED**

Run: `.venv/bin/python -m unittest tests.test_enterprise_gateway.DocumentationContractTest -v`

Expected: the new tool documentation assertions fail.

- [ ] **Step 3: Update README**

Document that `get_monthly_emission_trend_chart` maps to `GET /analysis/scopeEmissionForMonth`, list the seven new tools and paths, describe exact-only site resolution, three preparation calls, one final tool, model isolation, and Japanese/Chinese/English examples.

- [ ] **Step 4: Run full automated verification**

```bash
.venv/bin/python -m unittest discover -s tests -q
.venv/bin/python -m compileall -q ai_native app.py
awk '/<script>/{flag=1;next}/<\/script>/{flag=0}flag' ai_native/demo.html | node --check
git diff --check
```

Expected: all tests pass and every command exits 0.

- [ ] **Step 5: Run authenticated local acceptance**

Submit through the existing demo:

```text
2025年 Scope 1 の月別排出量推移を表示して
东京据点 2025 年每月排出量趋势
2024/04から2025/03と2025/04から2026/03の排出量を比較して
```

Verify one LLM planning call, bounded preparation calls, one final `/analysis/*` call, CMPF HTTP 200, no Token/full response in logs, and chart plus table fallback in the browser.

- [ ] **Step 6: Commit**

```bash
git add README.md tests/test_enterprise_gateway.py tests/test_cmpf_agent.py
git commit -m "docs: document CMPF emission analysis loops"
```

## Final Review Checklist

- [ ] `git status --short` contains no accidental CMPF changes.
- [ ] Every new final tool writes success or failure audit metadata.
- [ ] Unknown/ambiguous site names make zero final analysis calls.
- [ ] Chart titles include company and year/period; site details include site name.
- [ ] No test, log, exception, message, or checkpoint contains a Bearer Token.
- [ ] All implementation changes remain in `AI_Native`.
