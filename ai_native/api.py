from __future__ import annotations

from typing import List, Optional

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field

from ai_native.agent.graph import build_graph
from ai_native.gateway.cmpf_client import CmpfGateway
from ai_native.gateway.registry import ToolRegistry


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1)
    company_id: Optional[str] = None
    year: Optional[int] = None
    user_id: str = "local-user"
    tenant_id: str = "local"
    permissions: List[str] = Field(default_factory=lambda: ["cmpf:read"])


class ChatResponse(BaseModel):
    answer: str


def create_app() -> FastAPI:
    gateway = CmpfGateway()
    graph = build_graph(gateway)
    registry = ToolRegistry(gateway)

    app = FastAPI(title="CMPF AI Native Agent", version="0.1.0")

    @app.get("/", response_class=HTMLResponse)
    def index():
        return """
<!doctype html>
<html lang="zh">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>CMPF AI Native Agent</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; background: #f6f7f9; color: #17202a; }
    main { max-width: 860px; margin: 0 auto; padding: 32px 20px; }
    h1 { font-size: 28px; margin: 0 0 20px; }
    #messages { min-height: 360px; background: #fff; border: 1px solid #d8dee6; border-radius: 8px; padding: 16px; overflow: auto; }
    .msg { white-space: pre-wrap; margin: 0 0 14px; line-height: 1.55; }
    .user { color: #0f5c9c; }
    .agent { color: #1f2933; }
    form { display: flex; gap: 8px; margin-top: 12px; }
    input { flex: 1; padding: 12px; border: 1px solid #c7d0da; border-radius: 6px; font-size: 15px; }
    button { padding: 0 18px; border: 0; border-radius: 6px; background: #165dff; color: #fff; font-size: 15px; cursor: pointer; }
    button:disabled { opacity: .6; cursor: wait; }
  </style>
</head>
<body>
  <main>
    <h1>CMPF AI Native Agent</h1>
    <section id="messages"></section>
    <form id="chat-form">
      <input id="message" autocomplete="off" placeholder="例如：帮我查一下 cmpf-demo 公司 2025 年碳排放情况" />
      <button id="send" type="submit">发送</button>
    </form>
  </main>
  <script>
    const messages = document.querySelector("#messages");
    const form = document.querySelector("#chat-form");
    const input = document.querySelector("#message");
    const send = document.querySelector("#send");

    function append(role, text) {
      const node = document.createElement("div");
      node.className = "msg " + role;
      node.textContent = (role === "user" ? "你：\\n" : "Agent：\\n") + text;
      messages.appendChild(node);
      messages.scrollTop = messages.scrollHeight;
    }

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const text = input.value.trim();
      if (!text) return;
      append("user", text);
      input.value = "";
      send.disabled = true;
      try {
        const response = await fetch("/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ message: text, company_id: "cmpf-demo", year: 2025, permissions: ["cmpf:read"] })
        });
        const data = await response.json();
        append("agent", data.answer || JSON.stringify(data));
      } catch (error) {
        append("agent", "请求失败：" + error);
      } finally {
        send.disabled = false;
        input.focus();
      }
    });
  </script>
</body>
</html>
"""

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/tools")
    def tools():
        return {
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "required_permission": tool.required_permission,
                    "read_only": tool.read_only,
                }
                for tool in registry.list_tools()
            ]
        }

    @app.post("/chat", response_model=ChatResponse)
    def chat(request: ChatRequest):
        result = graph.invoke(
            {
                "messages": [HumanMessage(content=request.message)],
                "company_id": request.company_id,
                "year": request.year,
                "user_id": request.user_id,
                "tenant_id": request.tenant_id,
                "permissions": request.permissions,
            }
        )
        return ChatResponse(answer=result["messages"][-1].content)

    return app


app = create_app()
