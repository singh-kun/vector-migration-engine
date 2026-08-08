from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from vme.errors import ConfigurationError, redact_text
from vme.server.app import create_app
from vme.server.configuration import resolved_migration_settings
from vme.server.models import DesiredState, PlanStatus, ProfileRole, ServiceJobStatus
from vme.server.secrets import SecretResolver
from vme.server.security import EndpointPolicy
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
            allowed_adapters=("memory", "chroma", "qdrant"),
            allowed_data_roots=(Path(self.temporary.name),),
            allowed_endpoints=("qdrant.test:6333",),
            allowed_secret_env_names=("QDRANT_API_KEY",),
        )
        self.endpoint_policy = EndpointPolicy(
            allowed_adapters=self.settings.allowed_adapters,
            allowed_data_roots=self.settings.allowed_data_roots,
            allowed_endpoints=self.settings.allowed_endpoints,
            allow_insecure_endpoints=False,
            allow_embedded_chroma=True,
        )
        self.worker = ServiceWorker(
            store=self.store,
            state_path=str(self.state_path),
            resolver=SecretResolver(),
            poll_seconds=0.01,
            lease_seconds=30,
            worker_id="test-worker",
            endpoint_policy=self.endpoint_policy,
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

    def test_ssrf_local_paths_and_plaintext_headers_are_denied(self) -> None:
        blocked_connections = (
            {
                "name": "metadata-service",
                "adapter": "qdrant",
                "connection": {"url": "https://169.254.169.254:443"},
            },
            {
                "name": "outside-root",
                "adapter": "qdrant",
                "connection": {"path": str(Path(self.temporary.name).parent / "outside")},
            },
            {
                "name": "plaintext-header",
                "adapter": "chroma",
                "connection": {
                    "host": "qdrant.test",
                    "port": 6333,
                    "ssl": True,
                    "headers": {"X-Api-Key": "plaintext"},
                },
            },
        )
        for index, body in enumerate(blocked_connections):
            response = self.client.post(
                "/v1/connection-profiles",
                headers={"Idempotency-Key": f"blocked-endpoint-{index}"},
                json=body,
            )
            self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.store.list_profiles("default"), [])

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
                "connection": {
                    "url": "https://qdrant.test:6333",
                    "api_key": "env:QDRANT_API_KEY",
                },
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(
            response.json()["connection"]["api_key"],
            {"secret_ref": "env:<redacted>"},
        )
        fetched = self.client.get(f"/v1/connection-profiles/{response.json()['id']}")
        self.assertEqual(fetched.json()["connection"], response.json()["connection"])

    def test_unapproved_environment_secret_references_are_rejected(self) -> None:
        response = self.client.post(
            "/v1/connection-profiles",
            headers={"Idempotency-Key": "unapproved-secret-reference"},
            json={
                "name": "unapproved-secret",
                "adapter": "qdrant",
                "connection": {
                    "url": "https://qdrant.test:6333",
                    "api_key": "env:UNAPPROVED_SECRET",
                },
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.store.list_profiles("default"), [])

    def test_migration_resources_cannot_smuggle_secret_material(self) -> None:
        source_id = self._profile("secret-source", "source", "secret-source-key")
        destination_id = self._profile(
            "secret-destination", "destination", "secret-destination-key"
        )
        response = self.client.post(
            "/v1/migrations",
            headers={"Idempotency-Key": "secret-migration-key"},
            json={
                "name": "unsafe-migration",
                "source_profile_id": source_id,
                "destination_profile_id": destination_id,
                "source_resource": {"collection": "source", "api_key": "env:STOLEN"},
                "destination_resource": {"collection": "target"},
            },
        )
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("env:STOLEN", self.state_path.read_bytes().decode("latin1"))

    def test_security_headers_docs_host_and_body_limits_are_enforced(self) -> None:
        response = self.client.get("/v1/adapters")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertEqual(self.client.get("/docs").status_code, 404)
        self.assertEqual(
            self.client.get("/v1/adapters", headers={"Host": "attacker.example"}).status_code,
            400,
        )
        wrong_content_type = self.client.post(
            "/v1/connection-profiles",
            headers={
                "Content-Type": "text/plain",
                "Idempotency-Key": "wrong-content-type",
            },
            content=b"{}",
        )
        self.assertEqual(wrong_content_type.status_code, 415)

        limited = ServerSettings(
            state_path=Path(self.temporary.name) / "limited.sqlite3",
            auth_mode="none",
            run_worker=False,
            allowed_adapters=("memory",),
            max_request_bytes=1024,
        )
        with TestClient(create_app(limited)) as client:
            oversized = client.post(
                "/v1/connection-profiles",
                headers={"Idempotency-Key": "oversized-request-key"},
                json={
                    "name": "oversized",
                    "adapter": "memory",
                    "connection": {"padding": "x" * 2048},
                },
            )
            self.assertEqual(oversized.status_code, 413)

    def test_idempotency_keys_are_hashed_at_rest(self) -> None:
        raw_key = "never-store-this-idempotency-key"
        response = self.client.post(
            "/v1/connection-profiles",
            headers={"Idempotency-Key": raw_key},
            json={"name": "hashed-key", "adapter": "memory", "connection": {}},
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertNotIn(raw_key, self.state_path.read_bytes().decode("latin1"))

    def test_resolved_secret_values_are_available_for_error_redaction(self) -> None:
        secret = "provider-secret-value-123456"
        previous = os.environ.get("VME_TEST_PROVIDER_SECRET")
        os.environ["VME_TEST_PROVIDER_SECRET"] = secret
        try:
            values: set[str] = set()
            resolved = SecretResolver(
                allowed_environment_names=("VME_TEST_PROVIDER_SECRET",)
            ).resolve("env:VME_TEST_PROVIDER_SECRET", secret_values=values)
            self.assertEqual(resolved, secret)
            self.assertNotIn(secret, redact_text(f"provider echoed {secret}", values))
        finally:
            if previous is None:
                os.environ.pop("VME_TEST_PROVIDER_SECRET", None)
            else:
                os.environ["VME_TEST_PROVIDER_SECRET"] = previous

    def test_worker_revalidates_persisted_profiles_before_resolving_them(self) -> None:
        source = self.store.create_profile(
            workspace_id="default",
            name="tampered-source",
            adapter="qdrant",
            role=ProfileRole.SOURCE,
            connection={
                "url": "https://qdrant.test:6333",
                "api_key": "plaintext-from-tampered-state",
            },
            actor="test",
        )
        destination_id = self._profile(
            "tampered-destination", "destination", "tampered-destination-key"
        )
        migration = self.store.create_migration(
            workspace_id="default",
            name="tampered-state",
            specification={
                "source_profile_id": source.id,
                "destination_profile_id": destination_id,
            },
            actor="test",
        )
        with self.assertRaises(ConfigurationError):
            resolved_migration_settings(
                self.store,
                migration,
                SecretResolver(allowed_environment_names=self.settings.allowed_secret_env_names),
                endpoint_policy=self.endpoint_policy,
            )

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
                api_token="correct-token-that-is-at-least-32-characters",
                run_worker=False,
            )
            with TestClient(create_app(settings)) as client:
                self.assertEqual(client.get("/v1/adapters").status_code, 401)
                accepted = client.get(
                    "/v1/adapters",
                    headers={
                        "Authorization": "Bearer correct-token-that-is-at-least-32-characters"
                    },
                )
                self.assertEqual(accepted.status_code, 200)

    def test_weak_static_tokens_and_insecure_oidc_urls_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least 32"):
            ServerSettings(auth_mode="token", api_token="weak").validate()
        with self.assertRaisesRegex(ValueError, "HTTPS URL"):
            ServerSettings(
                auth_mode="oidc",
                oidc_issuer="http://issuer.example",
                oidc_audience="vme",
                oidc_jwks_url="https://issuer.example/jwks",
            ).validate()

    def test_endpoint_policy_accepts_only_explicit_tls_destination(self) -> None:
        policy = EndpointPolicy(
            allowed_adapters=("qdrant",),
            allowed_data_roots=(),
            allowed_endpoints=("db.example:6333",),
            allow_insecure_endpoints=False,
            allow_embedded_chroma=False,
        )
        policy.validate_connection("qdrant", {"url": "https://db.example:6333"})
        with self.assertRaises(ConfigurationError):
            policy.validate_connection("qdrant", {"url": "http://db.example:6333"})
        with self.assertRaises(ConfigurationError):
            policy.validate_connection("qdrant", {"url": "https://169.254.169.254:6333"})


if __name__ == "__main__":
    unittest.main()
