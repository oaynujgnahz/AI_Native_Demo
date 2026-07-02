from __future__ import annotations

from langchain_core.messages import HumanMessage

from ai_native.agent.graph import build_graph
from ai_native.gateway.cmpf_client import CmpfGateway


def main() -> None:
    graph = build_graph(CmpfGateway())
    print("CMPF LangGraph Agent started. Type 'exit' to quit.")
    while True:
        user_text = input("> ").strip()
        if user_text.lower() in {"exit", "quit"}:
            break
        result = graph.invoke({"messages": [HumanMessage(content=user_text)]})
        print(result["messages"][-1].content)


if __name__ == "__main__":
    main()
