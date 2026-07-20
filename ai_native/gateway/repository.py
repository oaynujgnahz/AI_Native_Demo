from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import uuid4


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


class InMemoryConversationRepository:
    def __init__(self) -> None:
        self._conversations: Dict[str, Conversation] = {}
        self._messages: Dict[str, List[Message]] = {}
        self._audits: List[Dict[str, Any]] = []
        self._lock = threading.RLock()

    def create_conversation(self, user_id: str, company_id: str) -> Conversation:
        conversation = Conversation(str(uuid4()), user_id, company_id, _now())
        with self._lock:
            self._conversations[conversation.id] = conversation
            self._messages[conversation.id] = []
        return conversation

    def get_conversation(self, conversation_id: str) -> Optional[Conversation]:
        return self._conversations.get(conversation_id)

    def list_messages(self, conversation_id: str) -> List[Message]:
        return list(self._messages.get(conversation_id, []))

    def add_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        chart: Optional[Dict[str, Any]] = None,
    ) -> Message:
        message = Message(str(uuid4()), conversation_id, role, content, chart, _now())
        with self._lock:
            self._messages.setdefault(conversation_id, []).append(message)
        return message

    def delete_conversation(self, conversation_id: str) -> bool:
        with self._lock:
            existed = self._conversations.pop(conversation_id, None) is not None
            self._messages.pop(conversation_id, None)
        return existed

    def write_audit(self, entry: Dict[str, Any]) -> None:
        safe_entry = {
            key: value
            for key, value in entry.items()
            if key not in {"token", "auth_token", "raw_payload", "authorization"}
        }
        self._audits.append({"created_at": _now(), **safe_entry})

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
        return [Message(str(r[0]), str(r[1]), r[2], r[3], r[4], r[5].isoformat()) for r in rows]

    def add_message(self, conversation_id: str, role: str, content: str, chart=None) -> Message:
        message_id = str(uuid4())
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO agent_messages(id,conversation_id,role,content,chart)
                   VALUES (%s,%s,%s,%s,%s::jsonb) RETURNING created_at""",
                (message_id, conversation_id, role, content, json.dumps(chart) if chart else None),
            )
            created_at = cursor.fetchone()[0].isoformat()
        return Message(message_id, conversation_id, role, content, chart, created_at)

    def delete_conversation(self, conversation_id: str) -> bool:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM agent_conversations WHERE id=%s", (conversation_id,))
            return cursor.rowcount > 0

    def write_audit(self, entry: Dict[str, Any]) -> None:
        safe = {k: v for k, v in entry.items() if k not in {"token", "auth_token", "raw_payload", "authorization"}}
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

    def health(self) -> bool:
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                return cursor.fetchone()[0] == 1
        except Exception:
            return False


def build_repository_from_env():
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        return PostgresConversationRepository(database_url)
    return InMemoryConversationRepository()

