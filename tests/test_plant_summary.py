"""Tests for the consistent whole-plant summary."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest

from waterplant.console.plant import LATEST_SUMMARY_KEY, _summary_key
from waterplant.console.server import Server
from waterplant.console.seed import seed_defaults
from waterplant.filter import Bank
from waterplant.store import Store


class PlantSummaryCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(f"{self._tmp.name}/state.json")
        seed_defaults(self.store)
        self.server = Server(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def call(self, method: str, path: str, payload: dict | None = None):
        response = self.server.dispatch(method, path, payload)
        body = json.loads(response.body.decode("utf-8"))
        return response.status, body

    def test_read_endpoints_share_one_frozen_summary(self) -> None:
        status, status_payload = self.call("GET", "/status")
        self.assertEqual(status, 200)
        _, snapshot = self.call("GET", "/snapshot")
        _, descriptions = self.call("GET", "/describe")
        report_response = self.server.dispatch("GET", "/report")
        report_text = report_response.body.decode("utf-8")

        self.assertEqual(snapshot, status_payload["snapshot"])
        self.assertEqual(descriptions, status_payload["describe"])
        self.assertIn(f"summary_id={status_payload['summary_id']}", report_text)
        self.assertEqual(report_text, status_payload["report"])

    def test_summary_is_single_source_and_audited(self) -> None:
        status, body = self.call("POST", "/plant/summary", {"idempotency_key": "run-1"})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["order"], "treatment_line")
        self.assertEqual(body["policy"], "mark_missing_continue")
        self.assertEqual(len(body["components"]), 16)

        status, snapshot = self.call("GET", "/snapshot")
        self.assertEqual(status, 200)
        for name, component in body["components"].items():
            self.assertEqual(snapshot[name], component["state"])

        status, descriptions = self.call("GET", "/describe")
        self.assertEqual(status, 200)
        for name, component in body["components"].items():
            self.assertEqual(descriptions[name], component["description"])

        self.assertIn(f"summary_id={body['summary_id']}", body["report"])
        self.assertIn(f"audit_record id={body['audit']['id']}", body["report"])

        status, latest = self.call("GET", "/plant/summary")
        self.assertEqual(status, 200)
        self.assertEqual(latest["summary_id"], body["summary_id"])

        status, audit = self.call("GET", "/plant/summary/audit")
        self.assertEqual(status, 200)
        self.assertEqual(audit["count"], 1)
        self.assertEqual(audit["entries"][0]["id"], body["audit"]["id"])
        self.assertEqual(audit["entries"][0]["summary_id"], body["summary_id"])
        self.assertEqual(audit["entries"][0]["fingerprint"], body["fingerprint"])

        status, chemical_audit = self.call("GET", "/audit/summary")
        self.assertEqual(status, 200)
        self.assertEqual(chemical_audit["count"], 0)

    def test_idempotent_retry_replays_unchanged_summary(self) -> None:
        status, first = self.call("POST", "/plant/summary", {"idempotency_key": "same"})
        self.assertEqual((status, first["replayed"]), (200, False))
        status, second = self.call("POST", "/plant/summary", {"idempotency_key": "same"})
        self.assertEqual(status, 200)
        self.assertTrue(second["replayed"])
        self.assertEqual(second["summary_id"], first["summary_id"])
        self.assertEqual(second["audit"]["id"], first["audit"]["id"])

        _, audit = self.call("GET", "/plant/summary/audit")
        self.assertEqual(audit["count"], 1)

    def test_retry_recovers_interrupted_summary_payload(self) -> None:
        _, first = self.call("POST", "/plant/summary", {"idempotency_key": "interrupted"})
        self.store.delete(_summary_key(first["summary_id"]))
        self.store.delete(LATEST_SUMMARY_KEY)

        recovered_server = Server(self.store)
        response = recovered_server.dispatch(
            "POST", "/plant/summary", {"idempotency_key": "interrupted"}
        )
        recovered = json.loads(response.body.decode("utf-8"))

        self.assertEqual(response.status, 200)
        self.assertTrue(recovered["replayed"])
        self.assertEqual(recovered["summary_id"], first["summary_id"])
        self.assertEqual(recovered["audit"]["id"], first["audit"]["id"])
        _, audit = self.call("GET", "/plant/summary/audit")
        self.assertEqual(audit["count"], 1)

    def test_empty_components_remain_zero(self) -> None:
        status, body = self.call("POST", "/plant/summary", {"idempotency_key": "empty"})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["telemetry"]["filter_beds"], 0)
        self.assertEqual(body["snapshot"]["filter"]["count"], 0)
        self.assertIn("filter beds=0", body["report"])

    def test_missing_component_is_marked_and_not_reported_as_zero(self) -> None:
        self.server.runtime.bank = None

        status, body = self.call("POST", "/plant/summary", {"idempotency_key": "missing"})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "degraded")
        self.assertIn("filter", body["missing"])
        self.assertIn("schedule", body["failed"])
        self.assertEqual(body["snapshot"]["filter"]["status"], "missing")
        self.assertIn("component missing", body["describe"]["filter"])
        self.assertIn("filter beds=MISSING", body["report"])
        self.assertIsNone(body["telemetry"]["filter_beds"])

        audit_entry = self.store.get("plant:summary:audit")[0]
        self.assertIn("degraded", audit_entry)

    def test_failed_component_is_distinct_from_missing_and_recovers(self) -> None:
        def fail() -> None:
            raise RuntimeError("filter read unavailable")

        self.server.runtime.bank.state = fail  # type: ignore[method-assign]
        status, failed = self.call("POST", "/plant/summary", {"idempotency_key": "failure"})
        self.assertEqual(status, 200)
        self.assertEqual(failed["failed"], ["filter"])
        self.assertEqual(failed["missing"], [])
        self.assertEqual(failed["snapshot"]["filter"]["status"], "failed")

        self.server.runtime.bank = Bank()
        status, recovered = self.call("POST", "/plant/summary", {"idempotency_key": "failure"})
        self.assertEqual(status, 409)
        self.assertEqual(
            recovered["error"],
            "idempotency key was already used for different plant state",
        )

        status, recovered = self.call("POST", "/plant/summary", {"idempotency_key": "recovered"})
        self.assertEqual(status, 200)
        self.assertEqual(recovered["status"], "ok")
        self.assertEqual(recovered["failed"], [])

    def test_component_updates_cannot_interleave_with_collection(self) -> None:
        barrier = threading.Barrier(2)
        observed_filter_counts: list[int] = []

        def collect() -> None:
            barrier.wait()
            summary = self.server.plant_summary.collect("thread-summary")[0]
            observed_filter_counts.append(summary.telemetry["filter_beds"])  # type: ignore[arg-type]

        def mutate() -> None:
            barrier.wait()
            with self.server.runtime.guard:
                self.server.runtime.bank.add_bed("race-bed", 1, 1.0)

        collector = threading.Thread(target=collect)
        mutator = threading.Thread(target=mutate)
        collector.start()
        mutator.start()
        collector.join()
        mutator.join()

        self.assertIn(observed_filter_counts[0], (0, 1))
        status, latest = self.call("GET", "/plant/summary")
        self.assertEqual(status, 200)
        self.assertIn(latest["telemetry"]["filter_beds"], (0, 1))


if __name__ == "__main__":
    unittest.main()
