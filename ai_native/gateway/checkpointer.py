from __future__ import annotations

import atexit
import os
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from ai_native.agent.actions import (
    AgentAction,
    SafeCandidate,
    SafeObservation,
    SafeObservationFacts,
)
from ai_native.agent.budgets import AgentBudgets, RunCounters


_CHECKPOINT_TYPES = (
    AgentAction,
    SafeCandidate,
    SafeObservation,
    SafeObservationFacts,
    AgentBudgets,
    RunCounters,
)


def build_checkpoint_serializer() -> JsonPlusSerializer:
    """Allow only the application types intentionally present in AgentState."""

    symbols = tuple((item.__module__, item.__name__) for item in _CHECKPOINT_TYPES)
    return JsonPlusSerializer(
        allowed_msgpack_modules=_CHECKPOINT_TYPES,
        allowed_json_modules=symbols,
    )


def build_checkpointer_from_env() -> Any:
    database_url = os.getenv("DATABASE_URL")
    serializer = build_checkpoint_serializer()
    if not database_url:
        return InMemorySaver(serde=serializer)

    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg import Connection
    from psycopg.rows import dict_row

    connection = Connection.connect(
        database_url,
        autocommit=True,
        prepare_threshold=0,
        row_factory=dict_row,
    )
    try:
        saver = PostgresSaver(connection, serde=serializer)
        saver.setup()
    except Exception:
        connection.close()
        raise
    atexit.register(connection.close)
    return saver
