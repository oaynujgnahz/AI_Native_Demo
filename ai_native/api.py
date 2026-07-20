from __future__ import annotations

import json
import os
import inspect
import logging
from time import perf_counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
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
    RequestValidationError,
    _select_tool,
)
from ai_native.agent.actions import AgentAction
from ai_native.agent.llm import ToolCallDecision
from ai_native.agent.runtime import AgentRuntimeResult, build_agent_runtime
from ai_native.gateway.checkpointer import build_checkpointer_from_env
from ai_native.gateway.errors import GatewayAgentError
from ai_native.gateway.runtime_context import RuntimeContext
from ai_native.gateway.tooling import ToolCatalog, build_enterprise_catalog
from ai_native.logging_config import configure_logging
from ai_native.observability import bind_log_context, clear_log_context

load_dotenv()

logger = logging.getLogger(__name__)


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


class _LegacyActionPlanner:
    """Adapt the previous one-shot planner contract during the API migration."""

    def __init__(self, planner: Any, catalog: ToolCatalog) -> None:
        self.planner = planner
        self.catalog = catalog

    def plan(
        self,
        *,
        goal: str,
        trusted_context: Mapping[str, Any],
        observations: Sequence[Any],
        artifact_summaries: Sequence[Mapping[str, Any]],
        remaining: Mapping[str, Any],
    ) -> AgentAction:
        del observations, remaining
        if artifact_summaries:
            return AgentAction(
                kind="finish",
                artifact_ids=[str(artifact_summaries[-1]["id"])],
            )

        decision = ToolCallDecision()
        if self.planner is not None:
            try:
                decision = self.planner.plan(
                    goal,
                    self.catalog,
                    context=dict(trusted_context),
                )
            except Exception:
                decision = ToolCallDecision()

        selected = decision.tool_name
        if selected not in self.catalog.names():
            selected = _select_tool(goal)
            arguments: dict[str, Any] = {}
        else:
            arguments = dict(decision.arguments)
        definition = self.catalog.get(selected)
        fields = definition.argument_model.model_fields
        if "company_id" in fields and "company_id" not in arguments:
            company_id = trusted_context.get("company_id")
            if company_id is not None:
                arguments["company_id"] = company_id
        if "year" in fields and "year" not in arguments:
            year = trusted_context.get("year")
            if year is not None:
                arguments["year"] = year
        return AgentAction(
            kind="call_tool",
            tool_name=selected,
            arguments=arguments,
            reason="legacy planner compatibility",
        )


def _is_runtime_planner(planner: Any) -> bool:
    try:
        parameters = inspect.signature(planner.plan).parameters.values()
    except (AttributeError, TypeError, ValueError):
        return False
    return any(item.name == "goal" for item in parameters) or any(
        item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters
    )


def create_app(
    planner=None,
    use_env_planner: bool = True,
    token_client=None,
    gateway=None,
    authenticator=None,
    repository=None,
    limiter=None,
    runtime=None,
    checkpointer=None,
) -> FastAPI:
    del token_client
    configure_logging()
    cmpf_gateway = gateway or CmpfGateway()
    conversation_repository = repository or build_repository_from_env()
    token_authenticator = authenticator or build_authenticator_from_env()
    request_limiter = limiter or RequestLimiter(
        per_minute=int(os.getenv("CMPF_AGENT_RATE_LIMIT_PER_MINUTE", "20")),
        concurrent=int(os.getenv("CMPF_AGENT_CONCURRENT_STREAMS", "2")),
    )
    tool_catalog = build_enterprise_catalog()
    runtime_planner = planner
    if planner is not None and not _is_runtime_planner(planner):
        runtime_planner = _LegacyActionPlanner(planner, tool_catalog)
    elif planner is None and not use_env_planner:
        runtime_planner = _LegacyActionPlanner(None, tool_catalog)
    selected_checkpointer = None
    if runtime is None:
        selected_checkpointer = checkpointer or build_checkpointer_from_env()
        agent_runtime = build_agent_runtime(
            planner=runtime_planner,
            gateway=cmpf_gateway,
            repository=conversation_repository,
            catalog=tool_catalog,
            checkpointer=selected_checkpointer,
        )
    else:
        agent_runtime = runtime

    app = FastAPI(title="CMPF Enterprise Agent Gateway", version="1.0.0")
    app.state.agent_runtime = agent_runtime
    app.state.checkpointer = selected_checkpointer
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

    @app.middleware("http")
    async def request_log_context(request: Request, call_next):
        trace_id = request.headers.get("X-Request-ID") or str(uuid4())
        token = bind_log_context(trace_id=trace_id, endpoint=request.url.path)
        started = perf_counter()
        try:
            response = await call_next(request)
            logger.info(
                "request completed",
                extra={
                    "status": response.status_code,
                    "duration": round(perf_counter() - started, 6),
                },
            )
            return response
        except Exception as exc:
            logger.error(
                "request failed",
                extra={
                    "status": 500,
                    "error": type(exc).__name__,
                    "duration": round(perf_counter() - started, 6),
                },
            )
            raise
        finally:
            clear_log_context(token)

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
        active_run = None
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
        conversation = _owned_conversation(
            conversation_repository, conversation_id, principal.user_id
        )
        if conversation.company_id != principal.company_id:
            raise HTTPException(
                status_code=404, detail={"code": "conversation_not_found"}
            )
        try:
            with request_limiter.limit(principal.user_id):
                allowed_company_ids = _allowed_company_ids(
                    cmpf_gateway, principal.company_id, bearer_token
                )
                pending_run = conversation_repository.get_pending_run(
                    conversation_id, principal.user_id
                )
                if pending_run is None:
                    trusted_context = _new_trusted_context(
                        request, principal, allowed_company_ids
                    )
                    active_run = conversation_repository.create_run(
                        conversation_id,
                        principal.user_id,
                        principal.company_id,
                    )
                    conversation_repository.add_message(
                        conversation_id, "user", request.message
                    )
                    result = agent_runtime.invoke(
                        _runtime_context(
                            principal,
                            bearer_token,
                            conversation_repository,
                            active_run.id,
                        ),
                        request.message,
                        run_id=active_run.id,
                        conversation_id=conversation_id,
                        company_id=trusted_context["company_id"],
                        allowed_company_ids=trusted_context["allowed_company_ids"],
                        year=trusted_context.get("year"),
                        locale=trusted_context.get("locale"),
                    )
                else:
                    checkpoint_state = _checkpoint_state(
                        agent_runtime, pending_run.id
                    )
                    trusted_context = _resume_trusted_context(
                        request,
                        principal,
                        allowed_company_ids,
                        checkpoint_state,
                    )
                    active_run = conversation_repository.claim_run(
                        pending_run.id, pending_run.version
                    )
                    if active_run is None:
                        raise GatewayAgentError(
                            category="conflict", code="active_run_conflict"
                        )
                    conversation_repository.add_message(
                        conversation_id, "user", request.message
                    )
                    result = agent_runtime.resume(
                        _runtime_context(
                            principal,
                            bearer_token,
                            conversation_repository,
                            active_run.id,
                        ),
                        active_run.id,
                        request.message,
                        trusted_context=trusted_context,
                    )
        except (RequestLimitExceeded, ConcurrentLimitExceeded) as exc:
            raise HTTPException(
                status_code=429, detail={"code": "rate_limited"}
            ) from exc
        except CompanyForbiddenError as exc:
            _mark_run_failed(conversation_repository, active_run)
            raise HTTPException(
                status_code=403, detail={"code": "company_forbidden"}
            ) from exc
        except RequestValidationError as exc:
            _mark_run_failed(conversation_repository, active_run)
            detail = {"code": exc.code}
            if exc.candidates:
                detail["candidates"] = exc.candidates
            raise HTTPException(
                status_code=422, detail=detail
            ) from exc
        except GatewayAgentError as exc:
            _mark_run_failed(conversation_repository, active_run)
            raise _gateway_http_error(exc) from exc
        except HTTPException:
            _mark_run_failed(conversation_repository, active_run)
            raise
        except Exception as exc:
            _mark_run_failed(conversation_repository, active_run)
            raise HTTPException(
                status_code=502, detail={"code": "upstream_error"}
            ) from exc

        latest_run = conversation_repository.get_run(active_run.id)
        if latest_run is not None and latest_run.cancel_requested:
            result = AgentRuntimeResult(
                run_id=active_run.id,
                status="cancelled",
                error_code="cancelled",
            )

        if result.status == "clarification_required":
            waiting_run = conversation_repository.set_run_status(
                active_run.id,
                "waiting_for_user",
                expected_version=active_run.version,
            )
            if waiting_run is None:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "active_run_conflict", "run_id": active_run.id},
                )
            question = result.question or "追加情報を入力してください。"
            conversation_repository.add_message(
                conversation_id, "assistant", question
            )

            def clarification_events():
                yield _sse(
                    "status",
                    {"run_id": active_run.id, "state": "waiting_for_user"},
                )
                yield _sse(
                    "clarification",
                    {
                        "run_id": active_run.id,
                        "question": question,
                        "missing_fields": list(result.missing_fields),
                        "candidates": list(result.candidates),
                    },
                )

            return _streaming_response(clarification_events(), active_run.id)

        terminal_status = result.status
        transitioned = conversation_repository.set_run_status(
            active_run.id,
            terminal_status,
            expected_version=active_run.version,
        )
        if transitioned is None:
            current = conversation_repository.get_run(active_run.id)
            if current is None or not current.cancel_requested:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "active_run_conflict", "run_id": active_run.id},
                )
            result = AgentRuntimeResult(
                run_id=active_run.id,
                status="cancelled",
                error_code="cancelled",
            )
        _raise_for_runtime_result(result)

        chart_payload = result.chart.model_dump(mode="json") if result.chart else None
        assistant_message = conversation_repository.add_message(
            conversation_id, "assistant", result.answer, chart_payload
        )
        tool_name = _result_tool_name(
            conversation_repository, result, chart_payload
        )

        def events():
            yield _sse(
                "status",
                {
                    "run_id": active_run.id,
                    "state": "tool_completed",
                    "tool": tool_name,
                },
            )
            yield _sse(
                "answer.delta",
                {"run_id": active_run.id, "delta": result.answer},
            )
            if chart_payload:
                yield _sse(
                    "visualization",
                    {"run_id": active_run.id, **chart_payload},
                )
            yield _sse(
                "answer.completed",
                {
                    "run_id": active_run.id,
                    "message_id": assistant_message.id,
                    "tool": tool_name,
                },
            )

        return _streaming_response(events(), active_run.id)

    @app.get("/v1/conversations/{conversation_id}/runs/{run_id}")
    def get_run_status(
        conversation_id: str,
        run_id: str,
        identity: tuple[Principal, str] = Depends(principal_and_token),
    ):
        principal, _ = identity
        conversation = _owned_conversation(
            conversation_repository, conversation_id, principal.user_id
        )
        if conversation.company_id != principal.company_id:
            raise HTTPException(
                status_code=404, detail={"code": "conversation_not_found"}
            )
        run = _owned_run(
            conversation_repository,
            conversation_id,
            run_id,
            principal.user_id,
            principal.company_id,
        )
        return _public_run(run)

    @app.post(
        "/v1/conversations/{conversation_id}/runs/{run_id}/cancel",
        status_code=status.HTTP_202_ACCEPTED,
    )
    def cancel_run(
        conversation_id: str,
        run_id: str,
        identity: tuple[Principal, str] = Depends(principal_and_token),
    ):
        principal, _ = identity
        conversation = _owned_conversation(
            conversation_repository, conversation_id, principal.user_id
        )
        if conversation.company_id != principal.company_id:
            raise HTTPException(
                status_code=404, detail={"code": "conversation_not_found"}
            )
        _owned_run(
            conversation_repository,
            conversation_id,
            run_id,
            principal.user_id,
            principal.company_id,
        )
        try:
            run = conversation_repository.request_cancel(run_id, principal.user_id)
        except GatewayAgentError as exc:
            raise _gateway_http_error(exc) from exc
        return _public_run(run)

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


def _owned_run(
    repository,
    conversation_id: str,
    run_id: str,
    user_id: str,
    company_id: str,
):
    run = repository.get_run(run_id)
    if (
        run is None
        or run.user_id != user_id
        or run.company_id != company_id
        or run.conversation_id != conversation_id
    ):
        raise HTTPException(status_code=404, detail={"code": "run_not_found"})
    return run


def _mark_run_failed(repository: Any, run: Any) -> None:
    if run is None:
        return
    try:
        repository.set_run_status(
            run.id,
            "failed",
            expected_version=run.version,
        )
    except Exception:
        return


def _public_run(run: Any) -> dict[str, Any]:
    return {
        "run_id": run.id,
        "conversation_id": run.conversation_id,
        "status": run.status,
        "cancel_requested": run.cancel_requested,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
    }


def _allowed_company_ids(
    gateway: Any,
    principal_company_id: str,
    bearer_token: str,
) -> list[str]:
    try:
        payload = gateway.list_direct_child_companies(auth_token=bearer_token)
    except Exception as exc:
        raise _safe_upstream_error("list_direct_children", exc) from exc
    children = _response_body(payload)
    if not isinstance(children, list):
        children = []
    allowed = {str(principal_company_id)}
    for item in children:
        if not isinstance(item, Mapping):
            continue
        value = item.get("value") or item.get("companyId") or item.get("id")
        if value is not None:
            allowed.add(str(value))
    return sorted(allowed)


def _new_trusted_context(
    request: StreamMessageRequest,
    principal: Principal,
    allowed_company_ids: Sequence[str],
) -> dict[str, Any]:
    company_id = str(request.context.company_id or principal.company_id).strip()
    _require_allowed_company(company_id, allowed_company_ids)
    trusted: dict[str, Any] = {
        "company_id": company_id,
        "allowed_company_ids": list(allowed_company_ids),
        "locale": request.context.locale or principal.locale,
    }
    if request.context.year is not None:
        trusted["year"] = request.context.year
    return trusted


def _resume_trusted_context(
    request: StreamMessageRequest,
    principal: Principal,
    allowed_company_ids: Sequence[str],
    checkpoint_state: Mapping[str, Any],
) -> dict[str, Any]:
    checkpoint_company_id = str(
        checkpoint_state.get("company_id") or principal.company_id
    ).strip()
    _require_allowed_company(checkpoint_company_id, allowed_company_ids)
    company_id = str(
        request.context.company_id or checkpoint_company_id
    ).strip()
    _require_allowed_company(company_id, allowed_company_ids)
    trusted: dict[str, Any] = {
        "company_id": company_id,
        "allowed_company_ids": list(allowed_company_ids),
        "locale": (
            request.context.locale
            or checkpoint_state.get("locale")
            or principal.locale
        ),
    }
    year = request.context.year
    if year is None:
        year = checkpoint_state.get("year")
    if year is not None:
        trusted["year"] = int(year)
    return trusted


def _require_allowed_company(
    company_id: str, allowed_company_ids: Sequence[str]
) -> None:
    if company_id not in {str(item) for item in allowed_company_ids}:
        raise CompanyForbiddenError(company_id)


def _checkpoint_state(runtime: Any, run_id: str) -> Mapping[str, Any]:
    snapshot = runtime.graph.get_state(
        {"configurable": {"thread_id": run_id}}
    )
    values = getattr(snapshot, "values", None)
    if not isinstance(values, Mapping) or not values:
        raise GatewayAgentError(
            category="persistence", code="run_checkpoint_missing"
        )
    return values


def _runtime_context(
    principal: Principal,
    bearer_token: str,
    repository: Any,
    run_id: str,
) -> RuntimeContext:
    timeout_seconds = max(
        1, int(os.getenv("CMPF_AGENT_RUN_TIMEOUT_SECONDS", "45"))
    )
    return RuntimeContext(
        principal=principal,
        bearer_token=bearer_token,
        deadline=datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds),
        repository=repository,
        is_cancelled=lambda: repository.is_cancelled(run_id),
    )


def _result_tool_name(
    repository: Any,
    result: AgentRuntimeResult,
    chart_payload: Mapping[str, Any] | None,
) -> str | None:
    if chart_payload is not None:
        source = chart_payload.get("source")
        if isinstance(source, Mapping) and source.get("tool_name") is not None:
            return str(source["tool_name"])
    for artifact_id in reversed(result.artifact_ids):
        artifact = repository.get_artifact(result.run_id, artifact_id)
        if artifact is not None:
            return str(artifact.tool_name)
    return None


def _streaming_response(events: Any, run_id: str) -> StreamingResponse:
    return StreamingResponse(
        events,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "X-Agent-Run-Id": run_id,
            "Access-Control-Expose-Headers": "X-Agent-Run-Id",
        },
    )


def _raise_for_runtime_result(result: AgentRuntimeResult) -> None:
    if result.status == "completed":
        return
    code = result.error_code or result.status
    detail: dict[str, Any] = {"code": code, "run_id": result.run_id}
    if result.candidates:
        detail["candidates"] = list(result.candidates)
    if code == "company_forbidden":
        status_code = 403
    elif code == "active_run_conflict":
        status_code = 409
    elif code == "cancelled":
        status_code = 409
    elif result.status == "exhausted":
        status_code = 429
    elif (
        code.startswith("base_")
        or code.startswith("invalid_")
        or code.endswith("_required")
    ):
        status_code = 422
    else:
        status_code = 502
    raise HTTPException(status_code=status_code, detail=detail)


def _gateway_http_error(exc: GatewayAgentError) -> HTTPException:
    if exc.category == "not_found":
        status_code = 404
    elif exc.category == "conflict":
        status_code = 409
    elif exc.category in {"authorization", "policy"}:
        status_code = 403
    elif exc.category == "validation":
        status_code = 422
    else:
        status_code = 502
    return HTTPException(status_code=status_code, detail={"code": exc.code})


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
