from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from vme.server.app import create_app
from vme.server.models import DesiredState, PlanStatus, ServiceJobStatus
from vme.server.secrets import SecretResolver
from vme.server.settings import ServerSettings
from vme.server.store import SQLiteServiceStore
from vme.server.worker import ServiceWorker


class ServiceApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.state_path = Path(self.temporary.name) / "service.sqlite3"
        self.store = SQLiteServiceStore(self.state_path)
        self.settings = ServerSettings(
            state_path=self.state_path,
            auth_mode="none",
            run_worker=False,
        )
        self.worker = ServiceWorker(
            store=self.store,
            state_path=str(self.state_path),
            resolver=SecretResolver(),
            poll_seconds=0.01,
            lease_seconds=30,
            worker_id="test-worker",
        )
        self.client_context = TestClient(
            create_app(self.settings, store=self.store, worker=self.worker)
        )
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.store.close()
        self.temporary.cleanup()

    def test_plaintext_secrets_are_rejected_before_persistence(self) -> None:
        response = self.client.post(
            "/v1/connection-profiles",
            headers={"Idempotency-Key": "plaintext-secret-test"},
            json={
                "name": "unsafe",
                "adapter": "qdrant",
                "connection": {"api_key": "do-not-store-me"},
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.store.list_profiles("default"), [])
        self.assertNotIn("do-not-store-me", self.state_path.read_bytes().decode("latin1"))

    def test_nested_and_url_credentials_are_rejected(self) -> None:
        for index, connection in enumerate(
            (
                {"nodes": [{"client_secret": "nested-secret"}]},
                {"url": "https://user:password@example.test"},
            )
        ):
            response = self.client.post(
                "/v1/connection-profiles",
                headers={"Idempotency-Key": f"unsafe-connection-{index}"},
                json={
                    "name": f"unsafe-{index}",
                    "adapter": "qdrant",
                    "connection": connection,
                },
            )
            self.assertEqual(response.status_code, 422)
        persisted = self.state_path.read_bytes().decode("latin1")
        self.assertNotIn("nested-secret", persisted)
        self.assertNotIn("password@example", persisted)

    def test_idempotent_resource_creation_returns_the_original_profile(self) -> None:
        request = {
            "name": "source",
            "adapter": "memory",
            "role": "source",
            "connection": {},
        }
        headers = {"Idempotency-Key": "same-profile-request"}
        first = self.client.post("/v1/connection-profiles", headers=headers, json=request)
        second = self.client.post("/v1/connection-profiles", headers=headers, json=request)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertEqual(first.json()["id"], second.json()["id"])
        self.assertEqual(len(self.store.list_profiles("default")), 1)

    def test_secret_references_are_redacted_from_api_responses(self) -> None:
        response = self.client.post(
            "/v1/connection-profiles",
            headers={"Idempotency-Key": "redacted-profile-key"},
            json={
                "name": "redacted",
                "adapter": "qdrant",
                "connection": {"api_key": "env:QDRANT_API_KEY"},
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(
            response.json()["connection"]["api_key"],
            {"secret_ref": "env:<redacted>"},
        )
        fetched = self.client.get(f"/v1/connection-profiles/{response.json()['id']}")
        self.assertEqual(fetched.json()["connection"], response.json()["connection"])

    def test_plan_and_job_run_as_durable_asynchronous_resources(self) -> None:
        source_id = self._profile("source", "source", "profile-source-key")
        destination_id = self._profile("destination", "destination", "profile-destination-key")
        migration = self.client.post(
            "/v1/migrations",
            headers={"Idempotency-Key": "migration-create-key"},
            json={
                "name": "service-memory-migration",
                "source_profile_id": source_id,
                "destination_profile_id": destination_id,
                "source_resource": {
                    "collection": "source",
                    "records": [
                        {
                            "id": "one",
                            "vectors": {"default": [0.1, 0.2, 0.3]},
                            "document": "durable asynchronous migration",
                            "metadata": {"kind": "test"},
                        }
                    ],
                },
                "destination_resource": {"collection": "target"},
                "mapping": {"ids": {"policy": "preserve"}},
                "verification": {"sample": {"records": 1}},
            },
        )
        self.assertEqual(migration.status_code, 201, migration.text)
        plan = self.client.post(
            "/v1/plans",
            headers={"Idempotency-Key": "plan-create-key"},
            json={"migration_id": migration.json()["id"]},
        )
        self.assertEqual(plan.status_code, 202, plan.text)
        self.assertEqual(plan.json()["status"], "queued")

        self.assertTrue(asyncio.run(self.worker.run_once()))
        planned = self.client.get(plan.headers["Location"])
        self.assertEqual(planned.json()["status"], PlanStatus.READY.value)
        self.assertTrue(planned.json()["fingerprint"])

        job = self.client.post(
            "/v1/jobs",
            headers={"Idempotency-Key": "job-create-key"},
            json={"plan_id": planned.json()["id"]},
        )
        self.assertEqual(job.status_code, 202, job.text)
        self.assertEqual(job.json()["status"], ServiceJobStatus.QUEUED.value)

        self.assertTrue(asyncio.run(self.worker.run_once()))
        completed = self.client.get(job.headers["Location"])
        self.assertEqual(completed.json()["status"], ServiceJobStatus.COMPLETED.value)
        self.assertEqual(completed.json()["progress"]["records_written"], 1)
        report = self.client.get(job.headers["Location"] + "/report")
        self.assertEqual(report.status_code, 200)
        self.assertEqual(report.json()["verification"]["mismatched_samples"], 0)
        events = self.client.get(job.headers["Location"] + "/events")
        self.assertEqual(events.status_code, 200)
        self.assertEqual(
            [item["event_type"] for item in events.json()["items"]],
            ["job_queued", "job_completed"],
        )

    def test_plan_lease_can_be_renewed_only_by_its_owner(self) -> None:
        source_id = self._profile("lease-source", "source", "lease-source-key")
        destination_id = self._profile("lease-destination", "destination", "lease-destination-key")
        migration = self.store.create_migration(
            workspace_id="default",
            name="lease-test",
            specification={
                "source_profile_id": source_id,
                "destination_profile_id": destination_id,
            },
            actor="test",
        )
        plan = self.store.create_plan(
            workspace_id="default", migration_id=migration.id, actor="test"
        )
        claim = self.store.claim_plan("worker", 30)
        assert claim is not None
        self.assertEqual(claim.resource_id, plan.id)
        self.assertTrue(self.store.renew_plan_lease(plan.id, claim.lease_token, 30))
        self.assertFalse(self.store.renew_plan_lease(plan.id, "wrong-token", 30))

    def test_queued_job_can_be_cancelled_without_running(self) -> None:
        source_id = self._profile("source", "source", "cancel-source-key")
        destination_id = self._profile("destination", "destination", "cancel-destination-key")
        migration = self.store.create_migration(
            workspace_id="default",
            name="cancel-me",
            specification={
                "source_profile_id": source_id,
                "destination_profile_id": destination_id,
                "source_resource": {"collection": "source", "records": []},
                "destination_resource": {"collection": "target"},
            },
            actor="test",
        )
        plan = self.store.create_plan(
            workspace_id="default", migration_id=migration.id, actor="test"
        )
        claim = self.store.claim_plan("test", 30)
        assert claim is not None
        self.store.finish_plan(
            plan_id=plan.id,
            lease_token=claim.lease_token,
            fingerprint="fingerprint",
            plan={"fingerprint": "fingerprint", "executable": True},
            executable=True,
        )
        job = self.store.create_job(workspace_id="default", plan_id=plan.id, actor="test")
        response = self.client.post(f"/v1/jobs/{job.id}/cancel")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], ServiceJobStatus.CANCELLED.value)
        self.assertEqual(response.json()["desired_state"], DesiredState.CANCELLED.value)

    def _profile(self, name: str, role: str, key: str) -> str:
        response = self.client.post(
            "/v1/connection-profiles",
            headers={"Idempotency-Key": key},
            json={"name": name, "adapter": "memory", "role": role, "connection": {}},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return str(response.json()["id"])


class AuthenticationTests(unittest.TestCase):
    def test_token_mode_rejects_missing_and_accepts_valid_bearer_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = ServerSettings(
                state_path=Path(directory) / "service.sqlite3",
                auth_mode="token",
                api_token="correct-token",
                run_worker=False,
            )
            with TestClient(create_app(settings)) as client:
                self.assertEqual(client.get("/v1/adapters").status_code, 401)
                accepted = client.get(
                    "/v1/adapters",
                    headers={"Authorization": "Bearer correct-token"},
                )
                self.assertEqual(accepted.status_code, 200)


if __name__ == "__main__":
    unittest.main()
