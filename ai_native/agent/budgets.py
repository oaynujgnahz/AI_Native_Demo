from dataclasses import dataclass, field

from ai_native.gateway.errors import GatewayAgentError


class BudgetExceeded(GatewayAgentError):
    def __init__(self, code: str) -> None:
        super().__init__(category="budget", code=code)


@dataclass(frozen=True)
class AgentBudgets:
    planner: int = 8
    tools: int = 6
    clarifications: int = 2
    request_timeout_seconds: int = 45


@dataclass
class RunCounters:
    budgets: AgentBudgets
    planner_calls: int = 0
    tool_calls: int = 0
    clarification_calls: int = 0
    tool_signatures: list[str] = field(default_factory=list)

    def consume_planner(self) -> None:
        if self.planner_calls >= self.budgets.planner:
            raise BudgetExceeded("planner_budget_exhausted")
        self.planner_calls += 1

    def consume_tool(self, signature: str) -> None:
        if signature in self.tool_signatures:
            raise BudgetExceeded("duplicate_tool_call")
        if self.tool_calls >= self.budgets.tools:
            raise BudgetExceeded("tool_budget_exhausted")
        self.tool_calls += 1
        self.tool_signatures.append(signature)

    def consume_clarification(self) -> None:
        if self.clarification_calls >= self.budgets.clarifications:
            raise BudgetExceeded("clarification_budget_exhausted")
        self.clarification_calls += 1
