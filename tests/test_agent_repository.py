from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone

from ai_native.gateway.observer import Artifact, ExecutionResult
from ai_native.gateway.repository import (
    InMemoryConversationRepository,
    PostgresConversationRepository,
)


class RunRepositoryContract:
    repository = None
    conversation_id = ""

    def test_one_active_or_waiting_run_per_conversation(self):
        run = self.repository.create_run(
            self.conversation_id, "user-1", "100"
        )
        self.assertEqual(run.status, "running")
        with self.assertRaises(Exception) as conflict:
            self.repository.create_run(self.conversation_id, "user-1", "100")
        self.assertEqual(conflict.exception.code, "active_run_conflict")

    def test_waiting_run_is_claimed_only_at_the_expected_version(self):
        run = self.repository.create_run(
            self.conversation_id, "user-1", "100"
        )
        waiting = self.repository.set_run_status(run.id, "waiting_for_user")

        self.assertIsNone(self.repository.claim_run(run.id, run.version))
        claimed = self.repository.claim_run(run.id, waiting.version)
        self.assertEqual(claimed.status, "running")
        self.assertEqual(claimed.version, waiting.version + 1)

    def test_cancel_is_persistent_and_owner_scoped(self):
        run = self.repository.create_run(
            self.conversation_id, "user-1", "100"
        )
        cancelled = self.repository.request_cancel(run.id, "user-1")
        self.assertEqual(cancelled.status, "cancelled")
        self.assertTrue(self.repository.is_cancelled(run.id))
        with self.assertRaises(Exception):
            self.repository.request_cancel(run.id, "other-user")

    def test_artifact_payload_is_persisted_outside_checkpoint_state(self):
        run = self.repository.create_run(
            self.conversation_id, "user-1", "100"
        )
        result = ExecutionResult(
            tool_name="get_company_info",
            endpoint="/user/company/100",
            safe_facts={"company_id": "100"},
            artifact=Artifact(
                id="artifact-1",
                kind="answer",
                payload={
                    "answer": "deterministic answer",
                    "authorization": "must-not-persist",
                },
            ),
            result_count=1,
        )

        result_id = self.repository.save_execution_result(run.id, result)
        restored = self.repository.get_execution_result(run.id, result_id)

        self.assertEqual(restored.artifact_payload, {"answer": "deterministic answer"})
        self.assertEqual(
            self.repository.get_artifact(run.id, "artifact-1").artifact_kind,
            "answer",
        )


class InMemoryRunRepositoryTest(RunRepositoryContract, unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 7, 20, tzinfo=timezone.utc)
        self.repository = InMemoryConversationRepository(clock=lambda: self.now)
        self.conversation_id = self.repository.create_conversation(
            "user-1", "100"
        ).id

    def test_cleanup_deletes_checkpoints_before_agent_rows_and_keeps_audit(self):
        run = self.repository.create_run(
            self.conversation_id, "user-1", "100"
        )
        self.repository.add_message(self.conversation_id, "user", "hello")
        self.repository.write_audit(
            {"user_id": "user-1", "company_id": "100", "status": "success"}
        )

        repository = self.repository

        class RecordingCheckpointer:
            deleted = []

            def delete_thread(self, run_id):
                self.deleted.append(run_id)
                assert repository.get_run(run_id) is not None

        checkpointer = RecordingCheckpointer()
        self.now += timedelta(days=8)
        deleted = self.repository.delete_expired_agent_data(checkpointer)

        self.assertEqual(checkpointer.deleted, [run.id])
        self.assertEqual(deleted["runs"], 1)
        self.assertEqual(deleted["messages"], 1)
        self.assertEqual(deleted["conversations"], 1)
        self.assertEqual(deleted["audits"], 0)
        self.assertEqual(len(self.repository._audits), 1)


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "TEST_DATABASE_URL is not set")
class PostgresRunRepositoryTest(RunRepositoryContract, unittest.TestCase):
    def setUp(self):
        self.repository = PostgresConversationRepository(
            os.environ["TEST_DATABASE_URL"]
        )
        self.conversation_id = self.repository.create_conversation(
            "user-1", "100"
        ).id

    def tearDown(self):
        self.repository.delete_conversation(self.conversation_id)


if __name__ == "__main__":
    unittest.main()
