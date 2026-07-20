from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from ai_native.gateway.auth import (
    AuthenticationError,
    Principal,
    build_authenticator_from_env,
)
from ai_native.gateway.cmpf_client import CmpfGateway
from ai_native.gateway.limits import (
    ConcurrentLimitExceeded,
    RequestLimitExceeded,
    RequestLimiter,
)
from ai_native.gateway.repository import build_repository_from_env
from ai_native.gateway.service import (
    CompanyForbiddenError,
    EnterpriseAgentService,
    RequestValidationError,
)
from ai_native.agent.llm import OpenAIToolPlanner
from ai_native.logging_config import configure_logging

load_dotenv()


class ConversationCreateRequest(BaseModel):
    model_config = {"extra": "ignore"}


class MessageContext(BaseModel):
    route_name: Optional[str] = Field(default=None, max_length=200)
    company_id: Optional[str] = Field(default=None, max_length=80)
    year: Optional[int] = Field(default=None, ge=2000, le=2100)
    locale: Optional[str] = Field(default=None, max_length=12)


class StreamMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    context: MessageContext = Field(default_factory=MessageContext)


def create_app(
    planner=None,
    use_env_planner: bool = True,
    token_client=None,
    gateway=None,
    authenticator=None,
    repository=None,
    limiter=None,
) -> FastAPI:
    del token_client
    configure_logging()
    cmpf_gateway = gateway or CmpfGateway()
    conversation_repository = repository or build_repository_from_env()
    token_authenticator = authenticator or build_authenticator_from_env()
    tool_planner = planner
    if tool_planner is None and use_env_planner:
        tool_planner = OpenAIToolPlanner.from_env()
    agent_service = EnterpriseAgentService(
        cmpf_gateway, conversation_repository, planner=tool_planner
    )
    request_limiter = limiter or RequestLimiter(
        per_minute=int(os.getenv("CMPF_AGENT_RATE_LIMIT_PER_MINUTE", "20")),
        concurrent=int(os.getenv("CMPF_AGENT_CONCURRENT_STREAMS", "2")),
    )

    app = FastAPI(title="CMPF Enterprise Agent Gateway", version="1.0.0")
    origins = [
        value.strip()
        for value in os.getenv("CMPF_AGENT_CORS_ORIGINS", "http://localhost:5173").split(",")
        if value.strip()
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )

    def principal_and_token(
        authorization: Optional[str] = Header(default=None),
    ) -> tuple[Principal, str]:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail={"code": "unauthorized"})
        token = authorization[7:].strip()
        if not token:
            raise HTTPException(status_code=401, detail={"code": "unauthorized"})
        try:
            principal = token_authenticator.authenticate(token)
        except (AuthenticationError, ValueError) as exc:
            raise HTTPException(
                status_code=401, detail={"code": "unauthorized"}
            ) from exc
        return principal, token

    @app.get("/", response_class=HTMLResponse)
    def demo_page():
        return Path(__file__).with_name("demo.html").read_text(encoding="utf-8")

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready():
        if not conversation_repository.health():
            raise HTTPException(status_code=503, detail={"code": "database_unavailable"})
        return {"status": "ready", "gateway_mode": cmpf_gateway.mode}

    @app.get("/health")
    def legacy_health():
        return {"status": "ok"}

    @app.get("/v1/public-config")
    def public_config():
        issuer = os.getenv("CMPF_KEYCLOAK_ISSUER", "").rstrip("/")
        client_id = os.getenv("CMPF_KEYCLOAK_CLIENT_ID", "CaM-js")
        oidc_base = f"{issuer}/protocol/openid-connect"
        return {
            "issuer": issuer,
            "client_id": client_id,
            "authorization_endpoint": f"{oidc_base}/auth",
            "token_endpoint": f"{oidc_base}/token",
            "end_session_endpoint": f"{oidc_base}/logout",
        }

    @app.get("/v1/cmpf/connection")
    def cmpf_connection(
        identity: tuple[Principal, str] = Depends(principal_and_token),
    ):
        principal, bearer_token = identity
        try:
            company_payload = cmpf_gateway.get_company_info(
                principal.company_id, auth_token=bearer_token
            )
        except Exception as exc:
            raise _safe_upstream_error("get_company_info", exc) from exc
        try:
            children_payload = cmpf_gateway.list_direct_child_companies(
                auth_token=bearer_token
            )
        except Exception as exc:
            raise _safe_upstream_error("list_direct_children", exc) from exc

        company = _response_body(company_payload)
        children = _response_body(children_payload)
        if not isinstance(company, dict):
            company = {}
        if not isinstance(children, list):
            children = []
        return {
            "status": "connected",
            "gateway_mode": cmpf_gateway.mode,
            "company_id": principal.company_id,
            "company_name": str(
                company.get("companyName") or company.get("name") or principal.company_id
            ),
            "direct_children": [
                {
                    "value": str(item.get("value") or item.get("companyId") or ""),
                    "label": str(item.get("label") or item.get("companyName") or ""),
                }
                for item in children
                if isinstance(item, dict)
            ],
        }

    @app.post("/v1/conversations", status_code=status.HTTP_201_CREATED)
    def create_conversation(
        request: ConversationCreateRequest,
        identity: tuple[Principal, str] = Depends(principal_and_token),
    ):
        del request
        principal, _ = identity
        conversation = conversation_repository.create_conversation(
            principal.user_id, principal.company_id
        )
        return {
            "id": conversation.id,
            "created_at": conversation.created_at,
            "company_id": conversation.company_id,
        }

    @app.get("/v1/conversations/{conversation_id}/messages")
    def get_messages(
        conversation_id: str,
        identity: tuple[Principal, str] = Depends(principal_and_token),
    ):
        principal, _ = identity
        conversation = _owned_conversation(
            conversation_repository, conversation_id, principal.user_id
        )
        del conversation
        messages = conversation_repository.list_messages(conversation_id)
        return {
            "conversation_id": conversation_id,
            "messages": [
                {
                    "id": message.id,
                    "role": message.role,
                    "content": message.content,
                    "chart": message.chart,
                    "created_at": message.created_at,
                }
                for message in messages
            ],
        }

    @app.post("/v1/conversations/{conversation_id}/messages/stream")
    def stream_message(
        conversation_id: str,
        request: StreamMessageRequest,
        identity: tuple[Principal, str] = Depends(principal_and_token),
    ):
        principal, bearer_token = identity
        _owned_conversation(conversation_repository, conversation_id, principal.user_id)
        context = request.context.model_dump(exclude_none=True)
        try:
            with request_limiter.limit(principal.user_id):
                result = agent_service.answer(
                    principal=principal,
                    bearer_token=bearer_token,
                    message=request.message,
                    context=context,
                )
        except (RequestLimitExceeded, ConcurrentLimitExceeded) as exc:
            raise HTTPException(
                status_code=429, detail={"code": "rate_limited"}
            ) from exc
        except CompanyForbiddenError as exc:
            raise HTTPException(
                status_code=403, detail={"code": "company_forbidden"}
            ) from exc
        except RequestValidationError as exc:
            detail = {"code": exc.code}
            if exc.candidates:
                detail["candidates"] = exc.candidates
            raise HTTPException(
                status_code=422, detail=detail
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail={"code": "upstream_error"}
            ) from exc

        conversation_repository.add_message(
            conversation_id, "user", request.message
        )
        chart_payload = result.chart.model_dump(mode="json") if result.chart else None
        assistant_message = conversation_repository.add_message(
            conversation_id, "assistant", result.answer, chart_payload
        )

        def events():
            yield _sse("status", {"state": "tool_completed", "tool": result.tool_name})
            yield _sse("answer.delta", {"delta": result.answer})
            if chart_payload:
                yield _sse("visualization", chart_payload)
            yield _sse(
                "answer.completed",
                {"message_id": assistant_message.id, "tool": result.tool_name},
            )

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    @app.delete(
        "/v1/conversations/{conversation_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def delete_conversation(
        conversation_id: str,
        identity: tuple[Principal, str] = Depends(principal_and_token),
    ):
        principal, _ = identity
        _owned_conversation(conversation_repository, conversation_id, principal.user_id)
        conversation_repository.delete_conversation(conversation_id)
        return Response(status_code=204)

    return app


def _owned_conversation(repository, conversation_id: str, user_id: str):
    conversation = repository.get_conversation(conversation_id)
    if conversation is None or conversation.user_id != user_id:
        raise HTTPException(
            status_code=404, detail={"code": "conversation_not_found"}
        )
    return conversation


def _sse(event: str, data: Dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _response_body(payload: Any) -> Any:
    if isinstance(payload, dict) and "body" in payload:
        return payload["body"]
    return payload


def _safe_upstream_error(stage: str, exc: Exception) -> HTTPException:
    response = getattr(exc, "response", None)
    upstream_status = getattr(response, "status_code", None)
    return HTTPException(
        status_code=502,
        detail={
            "code": "cmpf_upstream_error",
            "stage": stage,
            "upstream_status": upstream_status,
        },
    )


app = create_app()
