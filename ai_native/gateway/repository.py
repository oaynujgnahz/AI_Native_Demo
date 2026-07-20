from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Literal, Mapping, Optional
from uuid import uuid4

from ai_native.gateway.errors import GatewayAgentError


RunStatus = Literal[
    "running",
    "waiting_for_user",
    "completed",
    "failed",
    "cancelled",
    "exhausted",
]

RUN_STATUSES = frozenset(
    {
        "running",
        "waiting_for_user",
        "completed",
        "failed",
        "cancelled",
        "exhausted",
    }
)
ACTIVE_RUN_STATUSES = frozenset({"running", "waiting_for_user"})
_CONVERSATION_RETENTION = timedelta(days=7)
_AUDIT_RETENTION = timedelta(days=90)
_SENSITIVE_KEY = re.compile(
    r"token|authorization|cookie|raw(?:_|-)?payload", re.IGNORECASE
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _as_utc(value).isoformat()


def _safe_json(value: Any) -> Any:
    """Copy JSON-shaped values while removing sensitive keys at every depth."""

    if isinstance(value, Mapping):
        return {
            str(key): _safe_json(nested)
            for key, nested in value.items()
            if not _SENSITIVE_KEY.search(str(key))
        }
    if isinstance(value, (list, tuple)):
        return [_safe_json(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise GatewayAgentError(
        category="validation",
        code="unsafe_persisted_payload",
    )


def _validate_status(status: str) -> RunStatus:
    if status not in RUN_STATUSES:
        raise GatewayAgentError(category="validation", code="run_status_invalid")
    return status  # type: ignore[return-value]


@dataclass(frozen=True)
class Conversation:
    id: str
    user_id: str
    company_id: str
    created_at: str


@dataclass(frozen=True)
class Message:
    id: str
    conversation_id: str
    role: str
    content: str
    chart: Optional[Dict[str, Any]]
    created_at: str


@dataclass(frozen=True)
class AgentRun:
    id: str
    conversation_id: str
    user_id: str
    company_id: str
    status: RunStatus
    version: int
    cancel_requested: bool
    created_at: str
    updated_at: str
    expires_at: str


@dataclass(frozen=True)
class StoredExecutionResult:
    id: str
    run_id: str
    tool_name: str
    endpoint: str
    safe_facts: dict[str, Any]
    artifact_id: str
    artifact_kind: str
    artifact_payload: dict[str, Any]
    result_count: int
    audit_details: dict[str, Any]
    created_at: str
    expires_at: str


class InMemoryConversationRepository:
    def __init__(self, *, clock: Callable[[], datetime] = _utc_now) -> None:
        self._clock = clock
        self._conversations: Dict[str, Conversation] = {}
        self._conversation_expires_at: Dict[str, datetime] = {}
        self._messages: Dict[str, List[Message]] = {}
        self._audits: List[Dict[str, Any]] = []
        self._runs: Dict[str, AgentRun] = {}
        self._execution_results: Dict[str, StoredExecutionResult] = {}
        self._lock = threading.RLock()

    def _now(self) -> datetime:
        return _as_utc(self._clock())

    def create_conversation(self, user_id: str, company_id: str) -> Conversation:
        now = self._now()
        conversation = Conversation(str(uuid4()), user_id, company_id, _iso(now))
        with self._lock:
            self._conversations[conversation.id] = conversation
            self._conversation_expires_at[conversation.id] = (
                now + _CONVERSATION_RETENTION
            )
            self._messages[conversation.id] = []
        return conversation

    def get_conversation(self, conversation_id: str) -> Optional[Conversation]:
        with self._lock:
            expires_at = self._conversation_expires_at.get(conversation_id)
            if expires_at is not None and expires_at <= self._now():
                return None
            return self._conversations.get(conversation_id)

    def list_messages(self, conversation_id: str) -> List[Message]:
        with self._lock:
            return list(self._messages.get(conversation_id, []))

    def add_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        chart: Optional[Dict[str, Any]] = None,
    ) -> Message:
        message = Message(
            str(uuid4()), conversation_id, role, content, chart, _iso(self._now())
        )
        with self._lock:
            self._messages.setdefault(conversation_id, []).append(message)
        return message

    def delete_conversation(self, conversation_id: str) -> bool:
        with self._lock:
            existed = self._conversations.pop(conversation_id, None) is not None
            self._conversation_expires_at.pop(conversation_id, None)
            self._messages.pop(conversation_id, None)
            run_ids = {
                run.id
                for run in self._runs.values()
                if run.conversation_id == conversation_id
            }
            for run_id in run_ids:
                self._runs.pop(run_id, None)
            self._execution_results = {
                result_id: result
                for result_id, result in self._execution_results.items()
                if result.run_id not in run_ids
            }
        return existed

    def create_run(
        self, conversation_id: str, user_id: str, company_id: str
    ) -> AgentRun:
        with self._lock:
            if any(
                run.conversation_id == conversation_id
                and run.status in ACTIVE_RUN_STATUSES
                for run in self._runs.values()
            ):
                raise GatewayAgentError(
                    category="conflict", code="active_run_conflict"
                )
            now = self._now()
            expires_at = self._conversation_expires_at.get(
                conversation_id, now + _CONVERSATION_RETENTION
            )
            run = AgentRun(
                id=str(uuid4()),
                conversation_id=conversation_id,
                user_id=user_id,
                company_id=company_id,
                status="running",
                version=0,
                cancel_requested=False,
                created_at=_iso(now),
                updated_at=_iso(now),
                expires_at=_iso(expires_at),
            )
            self._runs[run.id] = run
            return run

    def get_run(self, run_id: str) -> Optional[AgentRun]:
        with self._lock:
            return self._runs.get(run_id)

    def claim_run(self, run_id: str, expected_version: int) -> Optional[AgentRun]:
        with self._lock:
            run = self._runs.get(run_id)
            if (
                run is None
                or run.version != expected_version
                or run.status != "waiting_for_user"
                or run.cancel_requested
            ):
                return None
            claimed = replace(
                run,
                status="running",
                version=run.version + 1,
                updated_at=_iso(self._now()),
            )
            self._runs[run_id] = claimed
            return claimed

    def set_run_status(
        self,
        run_id: str,
        status: RunStatus,
        expected_version: int | None = None,
    ) -> Optional[AgentRun]:
        selected_status = _validate_status(status)
        with self._lock:
            run = self._runs.get(run_id)
            if run is None or (
                expected_version is not None and run.version != expected_version
            ):
                return None
            updated = replace(
                run,
                status=selected_status,
                version=run.version + 1,
                updated_at=_iso(self._now()),
            )
            self._runs[run_id] = updated
            return updated

    def request_cancel(self, run_id: str, user_id: str) -> AgentRun:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None or run.user_id != user_id:
                raise GatewayAgentError(category="not_found", code="run_not_found")
            status: RunStatus = (
                "cancelled" if run.status in ACTIVE_RUN_STATUSES else run.status
            )
            updated = replace(
                run,
                status=status,
                cancel_requested=True,
                version=run.version + 1,
                updated_at=_iso(self._now()),
            )
            self._runs[run_id] = updated
            return updated

    def is_cancelled(self, run_id: str) -> bool:
        with self._lock:
            run = self._runs.get(run_id)
            return bool(run and run.cancel_requested)

    def get_pending_run(
        self, conversation_id: str, user_id: str | None = None
    ) -> Optional[AgentRun]:
        with self._lock:
            matching = [
                run
                for run in self._runs.values()
                if run.conversation_id == conversation_id
                and run.status == "waiting_for_user"
                and not run.cancel_requested
                and (user_id is None or run.user_id == user_id)
            ]
        return max(matching, key=lambda run: run.updated_at) if matching else None

    def save_execution_result(self, run_id: str, result: Any) -> str:
        artifact = getattr(result, "artifact", None)
        if artifact is None:
            raise GatewayAgentError(
                category="validation", code="execution_artifact_required"
            )
        safe_facts = _safe_json(dict(getattr(result, "safe_facts", {})))
        artifact_payload = _safe_json(dict(getattr(artifact, "payload", {})))
        audit_details = _safe_json(dict(getattr(result, "audit_details", {})))
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise GatewayAgentError(category="not_found", code="run_not_found")
            now = self._now()
            stored = StoredExecutionResult(
                id=str(uuid4()),
                run_id=run_id,
                tool_name=str(result.tool_name),
                endpoint=str(result.endpoint),
                safe_facts=safe_facts,
                artifact_id=str(artifact.id),
                artifact_kind=str(artifact.kind),
                artifact_payload=artifact_payload,
                result_count=int(result.result_count),
                audit_details=audit_details,
                created_at=_iso(now),
                expires_at=run.expires_at,
            )
            self._execution_results[stored.id] = stored
            return stored.id

    def get_execution_result(
        self, run_id: str, result_id: str
    ) -> Optional[StoredExecutionResult]:
        with self._lock:
            result = self._execution_results.get(result_id)
            return result if result and result.run_id == run_id else None

    def get_artifact(
        self, run_id: str, artifact_id: str
    ) -> Optional[StoredExecutionResult]:
        with self._lock:
            matching = [
                result
                for result in self._execution_results.values()
                if result.run_id == run_id and result.artifact_id == artifact_id
            ]
        return max(matching, key=lambda item: item.created_at) if matching else None

    def write_audit(self, entry: Dict[str, Any]) -> None:
        now = self._now()
        safe_entry = _safe_json(entry)
        with self._lock:
            self._audits.append(
                {
                    "created_at": _iso(now),
                    "expires_at": _iso(now + _AUDIT_RETENTION),
                    **safe_entry,
                }
            )

    def delete_expired_agent_data(
        self,
        checkpointer: Any | None = None,
        *,
        now: datetime | None = None,
    ) -> dict[str, int]:
        cutoff = _as_utc(now or self._now())
        with self._lock:
            expired_conversations = {
                conversation_id
                for conversation_id, expires_at in self._conversation_expires_at.items()
                if expires_at <= cutoff
            }
            expired_runs = {
                run.id
                for run in self._runs.values()
                if datetime.fromisoformat(run.expires_at) <= cutoff
                or run.conversation_id in expired_conversations
            }

        checkpoints_deleted = 0
        delete_thread = getattr(checkpointer, "delete_thread", None)
        if callable(delete_thread):
            for run_id in expired_runs:
                delete_thread(run_id)
                checkpoints_deleted += 1

        with self._lock:
            results_before = len(self._execution_results)
            self._execution_results = {
                result_id: result
                for result_id, result in self._execution_results.items()
                if result.run_id not in expired_runs
            }
            for run_id in expired_runs:
                self._runs.pop(run_id, None)
            messages_deleted = sum(
                len(self._messages.get(conversation_id, []))
                for conversation_id in expired_conversations
            )
            for conversation_id in expired_conversations:
                self._messages.pop(conversation_id, None)
                self._conversations.pop(conversation_id, None)
                self._conversation_expires_at.pop(conversation_id, None)
            audits_before = len(self._audits)
            self._audits = [
                item
                for item in self._audits
                if datetime.fromisoformat(str(item["expires_at"])) > cutoff
            ]
        return {
            "checkpoints": checkpoints_deleted,
            "execution_results": results_before - len(self._execution_results),
            "runs": len(expired_runs),
            "messages": messages_deleted,
            "conversations": len(expired_conversations),
            "audits": audits_before - len(self._audits),
        }

    def health(self) -> bool:
        return True


class PostgresConversationRepository:
    def __init__(self, database_url: str) -> None:
        import psycopg

        self.database_url = database_url
        self._psycopg = psycopg
        self._initialize()

    def _connect(self):
        return self._psycopg.connect(self.database_url)

    def _initialize(self) -> None:
        statements = """
        CREATE TABLE IF NOT EXISTS agent_conversations (
          id UUID PRIMARY KEY, user_id TEXT NOT NULL, company_id TEXT NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), expires_at TIMESTAMPTZ NOT NULL
        );
        CREATE TABLE IF NOT EXISTS agent_messages (
          id UUID PRIMARY KEY, conversation_id UUID NOT NULL REFERENCES agent_conversations(id) ON DELETE CASCADE,
          role TEXT NOT NULL, content TEXT NOT NULL, chart JSONB, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS agent_audit (
          id BIGSERIAL PRIMARY KEY, user_id TEXT NOT NULL, company_id TEXT,
          tool_name TEXT, status TEXT NOT NULL, metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), expires_at TIMESTAMPTZ NOT NULL
        );
        CREATE TABLE IF NOT EXISTS agent_runs (
          id UUID PRIMARY KEY,
          conversation_id UUID NOT NULL REFERENCES agent_conversations(id) ON DELETE CASCADE,
          user_id TEXT NOT NULL,
          company_id TEXT NOT NULL,
          status TEXT NOT NULL CHECK (status IN ('running','waiting_for_user','completed','failed','cancelled','exhausted')),
          version BIGINT NOT NULL DEFAULT 0,
          cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          expires_at TIMESTAMPTZ NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS agent_runs_one_active_conversation
          ON agent_runs(conversation_id)
          WHERE status IN ('running','waiting_for_user');
        CREATE TABLE IF NOT EXISTS agent_execution_results (
          id UUID PRIMARY KEY,
          run_id UUID NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
          tool_name TEXT NOT NULL,
          endpoint TEXT NOT NULL,
          safe_facts JSONB NOT NULL DEFAULT '{}'::jsonb,
          artifact_id TEXT NOT NULL,
          artifact_kind TEXT NOT NULL,
          artifact_payload JSONB NOT NULL,
          result_count INTEGER NOT NULL,
          audit_details JSONB NOT NULL DEFAULT '{}'::jsonb,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          expires_at TIMESTAMPTZ NOT NULL
        );
        CREATE INDEX IF NOT EXISTS agent_execution_results_artifact
          ON agent_execution_results(run_id, artifact_id);
        """
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(statements)

    def create_conversation(self, user_id: str, company_id: str) -> Conversation:
        conversation_id = str(uuid4())
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO agent_conversations(id,user_id,company_id,expires_at)
                   VALUES (%s,%s,%s,NOW()+INTERVAL '7 days') RETURNING created_at""",
                (conversation_id, user_id, company_id),
            )
            created_at = cursor.fetchone()[0].isoformat()
        return Conversation(conversation_id, user_id, company_id, created_at)

    def get_conversation(self, conversation_id: str) -> Optional[Conversation]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT id,user_id,company_id,created_at FROM agent_conversations WHERE id=%s AND expires_at>NOW()",
                (conversation_id,),
            )
            row = cursor.fetchone()
        return Conversation(str(row[0]), row[1], row[2], row[3].isoformat()) if row else None

    def list_messages(self, conversation_id: str) -> List[Message]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT id,conversation_id,role,content,chart,created_at FROM agent_messages WHERE conversation_id=%s ORDER BY created_at,id",
                (conversation_id,),
            )
            rows = cursor.fetchall()
        return [
            Message(str(r[0]), str(r[1]), r[2], r[3], r[4], r[5].isoformat())
            for r in rows
        ]

    def add_message(
        self, conversation_id: str, role: str, content: str, chart=None
    ) -> Message:
        message_id = str(uuid4())
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO agent_messages(id,conversation_id,role,content,chart)
                   VALUES (%s,%s,%s,%s,%s::jsonb) RETURNING created_at""",
                (
                    message_id,
                    conversation_id,
                    role,
                    content,
                    json.dumps(chart) if chart else None,
                ),
            )
            created_at = cursor.fetchone()[0].isoformat()
        return Message(message_id, conversation_id, role, content, chart, created_at)

    def delete_conversation(self, conversation_id: str) -> bool:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM agent_conversations WHERE id=%s", (conversation_id,)
            )
            return cursor.rowcount > 0

    def create_run(
        self, conversation_id: str, user_id: str, company_id: str
    ) -> AgentRun:
        run_id = str(uuid4())
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(
                    """INSERT INTO agent_runs(
                         id,conversation_id,user_id,company_id,status,expires_at)
                       SELECT %s,%s,%s,%s,'running',expires_at
                       FROM agent_conversations WHERE id=%s
                       RETURNING id,conversation_id,user_id,company_id,status,version,
                                 cancel_requested,created_at,updated_at,expires_at""",
                    (
                        run_id,
                        conversation_id,
                        user_id,
                        company_id,
                        conversation_id,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    raise GatewayAgentError(
                        category="not_found", code="conversation_not_found"
                    )
        except self._psycopg.errors.UniqueViolation as exc:
            raise GatewayAgentError(
                category="conflict", code="active_run_conflict"
            ) from exc
        return _run_from_row(row)

    def get_run(self, run_id: str) -> Optional[AgentRun]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT id,conversation_id,user_id,company_id,status,version,
                          cancel_requested,created_at,updated_at,expires_at
                   FROM agent_runs WHERE id=%s AND expires_at>NOW()""",
                (run_id,),
            )
            row = cursor.fetchone()
        return _run_from_row(row) if row else None

    def claim_run(self, run_id: str, expected_version: int) -> Optional[AgentRun]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """UPDATE agent_runs
                   SET status='running',version=version+1,updated_at=NOW()
                   WHERE id=%s AND version=%s AND status='waiting_for_user'
                     AND cancel_requested=FALSE AND expires_at>NOW()
                   RETURNING id,conversation_id,user_id,company_id,status,version,
                             cancel_requested,created_at,updated_at,expires_at""",
                (run_id, expected_version),
            )
            row = cursor.fetchone()
        return _run_from_row(row) if row else None

    def set_run_status(
        self,
        run_id: str,
        status: RunStatus,
        expected_version: int | None = None,
    ) -> Optional[AgentRun]:
        selected_status = _validate_status(status)
        version_clause = "" if expected_version is None else " AND version=%s"
        params: tuple[Any, ...] = (
            (selected_status, run_id)
            if expected_version is None
            else (selected_status, run_id, expected_version)
        )
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """UPDATE agent_runs
                   SET status=%s,version=version+1,updated_at=NOW()
                   WHERE id=%s"""
                + version_clause
                + """ RETURNING id,conversation_id,user_id,company_id,status,version,
                                 cancel_requested,created_at,updated_at,expires_at""",
                params,
            )
            row = cursor.fetchone()
        return _run_from_row(row) if row else None

    def request_cancel(self, run_id: str, user_id: str) -> AgentRun:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """UPDATE agent_runs
                   SET cancel_requested=TRUE,
                       status=CASE WHEN status IN ('running','waiting_for_user')
                                   THEN 'cancelled' ELSE status END,
                       version=version+1,updated_at=NOW()
                   WHERE id=%s AND user_id=%s AND expires_at>NOW()
                   RETURNING id,conversation_id,user_id,company_id,status,version,
                             cancel_requested,created_at,updated_at,expires_at""",
                (run_id, user_id),
            )
            row = cursor.fetchone()
        if row is None:
            raise GatewayAgentError(category="not_found", code="run_not_found")
        return _run_from_row(row)

    def is_cancelled(self, run_id: str) -> bool:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT cancel_requested FROM agent_runs WHERE id=%s", (run_id,)
            )
            row = cursor.fetchone()
        return bool(row and row[0])

    def get_pending_run(
        self, conversation_id: str, user_id: str | None = None
    ) -> Optional[AgentRun]:
        owner_clause = "" if user_id is None else " AND user_id=%s"
        params: tuple[Any, ...] = (
            (conversation_id,)
            if user_id is None
            else (conversation_id, user_id)
        )
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT id,conversation_id,user_id,company_id,status,version,
                          cancel_requested,created_at,updated_at,expires_at
                   FROM agent_runs
                   WHERE conversation_id=%s AND status='waiting_for_user'
                     AND cancel_requested=FALSE AND expires_at>NOW()"""
                + owner_clause
                + " ORDER BY updated_at DESC LIMIT 1",
                params,
            )
            row = cursor.fetchone()
        return _run_from_row(row) if row else None

    def save_execution_result(self, run_id: str, result: Any) -> str:
        artifact = getattr(result, "artifact", None)
        if artifact is None:
            raise GatewayAgentError(
                category="validation", code="execution_artifact_required"
            )
        result_id = str(uuid4())
        safe_facts = _safe_json(dict(getattr(result, "safe_facts", {})))
        artifact_payload = _safe_json(dict(getattr(artifact, "payload", {})))
        audit_details = _safe_json(dict(getattr(result, "audit_details", {})))
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO agent_execution_results(
                     id,run_id,tool_name,endpoint,safe_facts,artifact_id,
                     artifact_kind,artifact_payload,result_count,audit_details,expires_at)
                   SELECT %s,%s,%s,%s,%s::jsonb,%s,%s,%s::jsonb,%s,%s::jsonb,expires_at
                   FROM agent_runs WHERE id=%s AND expires_at>NOW()
                   RETURNING id""",
                (
                    result_id,
                    run_id,
                    str(result.tool_name),
                    str(result.endpoint),
                    json.dumps(safe_facts),
                    str(artifact.id),
                    str(artifact.kind),
                    json.dumps(artifact_payload),
                    int(result.result_count),
                    json.dumps(audit_details),
                    run_id,
                ),
            )
            if cursor.fetchone() is None:
                raise GatewayAgentError(category="not_found", code="run_not_found")
        return result_id

    def get_execution_result(
        self, run_id: str, result_id: str
    ) -> Optional[StoredExecutionResult]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT id,run_id,tool_name,endpoint,safe_facts,artifact_id,
                          artifact_kind,artifact_payload,result_count,audit_details,
                          created_at,expires_at
                   FROM agent_execution_results
                   WHERE id=%s AND run_id=%s AND expires_at>NOW()""",
                (result_id, run_id),
            )
            row = cursor.fetchone()
        return _execution_result_from_row(row) if row else None

    def get_artifact(
        self, run_id: str, artifact_id: str
    ) -> Optional[StoredExecutionResult]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT id,run_id,tool_name,endpoint,safe_facts,artifact_id,
                          artifact_kind,artifact_payload,result_count,audit_details,
                          created_at,expires_at
                   FROM agent_execution_results
                   WHERE run_id=%s AND artifact_id=%s AND expires_at>NOW()
                   ORDER BY created_at DESC LIMIT 1""",
                (run_id, artifact_id),
            )
            row = cursor.fetchone()
        return _execution_result_from_row(row) if row else None

    def write_audit(self, entry: Dict[str, Any]) -> None:
        safe = _safe_json(entry)
        user_id = str(safe.pop("user_id", ""))
        company_id = safe.pop("company_id", None)
        tool_name = safe.pop("tool_name", None)
        status = str(safe.pop("status", "unknown"))
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO agent_audit(user_id,company_id,tool_name,status,metadata,expires_at)
                   VALUES (%s,%s,%s,%s,%s::jsonb,NOW()+INTERVAL '90 days')""",
                (user_id, company_id, tool_name, status, json.dumps(safe)),
            )

    def delete_expired_agent_data(self) -> dict[str, int]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM agent_conversations WHERE expires_at<=NOW() FOR UPDATE"
            )
            conversation_ids = [str(row[0]) for row in cursor.fetchall()]
            cursor.execute(
                """SELECT id FROM agent_runs
                   WHERE expires_at<=NOW() OR conversation_id=ANY(%s::uuid[])
                   FOR UPDATE""",
                (conversation_ids,),
            )
            run_ids = [str(row[0]) for row in cursor.fetchall()]

            checkpoints_deleted = 0
            for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                cursor.execute("SELECT to_regclass(%s)", (table,))
                if cursor.fetchone()[0] is None or not run_ids:
                    continue
                cursor.execute(
                    f"DELETE FROM {table} WHERE thread_id=ANY(%s::text[])",
                    (run_ids,),
                )
                checkpoints_deleted += cursor.rowcount

            cursor.execute(
                "DELETE FROM agent_execution_results WHERE run_id=ANY(%s::uuid[])",
                (run_ids,),
            )
            results_deleted = cursor.rowcount
            cursor.execute(
                "DELETE FROM agent_runs WHERE id=ANY(%s::uuid[])", (run_ids,)
            )
            runs_deleted = cursor.rowcount
            cursor.execute(
                "DELETE FROM agent_messages WHERE conversation_id=ANY(%s::uuid[])",
                (conversation_ids,),
            )
            messages_deleted = cursor.rowcount
            cursor.execute(
                "DELETE FROM agent_conversations WHERE id=ANY(%s::uuid[])",
                (conversation_ids,),
            )
            conversations_deleted = cursor.rowcount
            cursor.execute("DELETE FROM agent_audit WHERE expires_at<=NOW()")
            audits_deleted = cursor.rowcount
        return {
            "checkpoints": checkpoints_deleted,
            "execution_results": results_deleted,
            "runs": runs_deleted,
            "messages": messages_deleted,
            "conversations": conversations_deleted,
            "audits": audits_deleted,
        }

    def health(self) -> bool:
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                return cursor.fetchone()[0] == 1
        except Exception:
            return False


def _run_from_row(row: Any) -> AgentRun:
    return AgentRun(
        id=str(row[0]),
        conversation_id=str(row[1]),
        user_id=row[2],
        company_id=row[3],
        status=_validate_status(row[4]),
        version=int(row[5]),
        cancel_requested=bool(row[6]),
        created_at=row[7].isoformat(),
        updated_at=row[8].isoformat(),
        expires_at=row[9].isoformat(),
    )


def _execution_result_from_row(row: Any) -> StoredExecutionResult:
    return StoredExecutionResult(
        id=str(row[0]),
        run_id=str(row[1]),
        tool_name=row[2],
        endpoint=row[3],
        safe_facts=dict(row[4]),
        artifact_id=row[5],
        artifact_kind=row[6],
        artifact_payload=dict(row[7]),
        result_count=int(row[8]),
        audit_details=dict(row[9]),
        created_at=row[10].isoformat(),
        expires_at=row[11].isoformat(),
    )


def build_repository_from_env():
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        return PostgresConversationRepository(database_url)
    return InMemoryConversationRepository()
