"""Tests for the single source plant status aggregation."""

from __future__ import annotations

import json
import tempfile
import unittest

from waterplant.console.server import Server
from waterplant.console.status import COMPONENT_ORDER, MISSING, StatusService, render_text
from waterplant.console.seed import seed_defaults
from waterplant.store import Store

COMPONENTS = {
    "pipeline",
    "store",
    "intake",
    "coag",
    "chlor",
    "filter",
    "backwash",
    "turbidity",
    "flow",
    "clearwell",
    "quota",
    "audit",
    "ph",
    "schedule",
    "trend",
    "inventory",
}


class StatusCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(f"{self._tmp.name}/state.json")
        seed_defaults(self.store)
        self.server = Server(self.store)
        self.service: StatusService = self.server.runtime.status

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def call(self, path: str, headers: dict[str, str] | None = None, key: str = ""):
        if key:
            headers = dict(headers or {})
            headers["Idempotency-Key"] = key
        response = self.server.dispatch("GET", path, headers=headers)
        body = response.body.decode("utf-8")
        parsed = json.loads(body) if response.content_type.startswith("application/json") else body
        return response.status, parsed

    def test_full_pass_covers_every_component_in_line_order(self) -> None:
        status = self.service.collect("a")
        self.assertEqual(status.complete, True)
        self.assertEqual(status.missing, ())
        self.assertEqual(tuple(c.name for c in status.components), COMPONENT_ORDER)
        self.assertEqual(set(status.snapshot_payload()), COMPONENTS)
        self.assertEqual(set(status.describe_payload()), COMPONENTS)

        envelope = status.envelope_payload()
        self.assertEqual(envelope["present"], 16)
        self.assertEqual(envelope["total"], 16)
        self.assertEqual(envelope["missing"], [])
        self.assertTrue(envelope["audit_id"])
        self.assertTrue(envelope["fingerprint"])
        for name in COMPONENTS:
            self.assertEqual(envelope["components"][name]["status"], "ok")

    def test_three_outputs_share_one_capture(self) -> None:
        # Same key through all three endpoints: one pass, one audit entry.
        _, snapshot = self.call("/snapshot", key="k")
        _, describe = self.call("/describe", key="k")
        status_code, report = self.call("/report", key="k")
        self.assertEqual(status_code, 200)

        status = self.service.collect("k")
        self.assertEqual(snapshot, status.snapshot_payload())
        self.assertEqual(describe, status.describe_payload())
        self.assertEqual(report, render_text(status))

        # Spot check that the same metric agrees in snapshot and description.
        self.assertIn(f"store keys={snapshot['store']['size']}", describe["store"])
        self.assertIn(f"store keys={snapshot['store']['size']}", report)
        self.assertIn(status.fingerprint, report)
        self.assertIn(status.audit_id, report)

    def test_unaudited_projection_for_internal_use(self) -> None:
        before = self.server.runtime.auditor.count()
        status = self.service.collect(audit=False)
        self.assertEqual(self.server.runtime.auditor.count(), before)
        self.assertFalse(status.audit_recorded)
        self.assertEqual(status.audit_id, "")

    def test_missing_component_is_marked_not_zero(self) -> None:
        self.service.fail_reader("quota", "accumulator bus offline")
        status = self.service.collect("b")

        self.assertFalse(status.complete)
        self.assertEqual(status.missing, ("quota",))
        self.assertIsNone(status.snapshot_payload()["quota"])
        self.assertIn("MISSING", status.describe_payload()["quota"])
        self.assertIn("accumulator bus offline", status.describe_payload()["quota"])
        self.assertIn("quota: MISSING", render_text(status))

        envelope = status.envelope_payload()
        self.assertEqual(envelope["components"]["quota"]["status"], MISSING)
        self.assertEqual(envelope["components"]["quota"]["data"], None)
        # Other components stay readable, the pass did not fail wholesale.
        self.assertIsNotNone(status.snapshot_payload()["ph"])

    def test_multiple_missing_components_continue_the_pass(self) -> None:
        self.service.fail_reader("quota", "offline")
        self.service.fail_reader("ph", "probe lost")
        status = self.service.collect("c")
        self.assertEqual(set(status.missing), {"quota", "ph"})
        self.assertEqual(status.present_count(), 14)
        text = render_text(status)
        self.assertIn("quota: MISSING", text)
        self.assertIn("ph: MISSING", text)

    def test_idempotent_key_returns_same_capture(self) -> None:
        first = self.service.collect("d")
        second = self.service.collect("d")
        self.assertIs(first, second)

    def test_failed_retry_is_idempotent_until_recovery(self) -> None:
        self.service.fail_reader("quota", "offline")
        first = self.service.collect("e")
        self.assertEqual(first.attempts, 1)
        audit_after_first = self.server.runtime.auditor.count()

        retried = self.service.collect("e")
        self.assertIs(retried, first)
        self.assertEqual(retried.attempts, 1)
        self.assertEqual(self.server.runtime.auditor.count(), audit_after_first)

    def test_recovery_rereads_only_missing_and_audits_once(self) -> None:
        self.service.fail_reader("quota", "offline")
        first = self.service.collect("f")
        captured_ph = first.component("ph")
        audit_count = self.server.runtime.auditor.count()

        self.service.restore_reader("quota")
        recovered = self.service.collect("f")
        self.assertTrue(recovered.complete)
        self.assertEqual(recovered.missing, ())
        self.assertEqual(recovered.attempts, 2)
        self.assertEqual(recovered.summary_id, first.summary_id)
        # Components that were fine are the exact captured objects, not re-read.
        self.assertIs(recovered.component("ph"), captured_ph)
        # Exactly one recovery audit entry was appended.
        self.assertEqual(self.server.runtime.auditor.count(), audit_count + 1)

        # After recovery the capture is cached and repeats stay silent.
        again = self.service.collect("f")
        self.assertIs(again, recovered)
        self.assertEqual(self.server.runtime.auditor.count(), audit_count + 1)

    def test_recovery_with_some_components_still_missing(self) -> None:
        self.service.fail_reader("quota", "offline")
        self.service.fail_reader("ph", "offline")
        first = self.service.collect("g")

        self.service.restore_reader("quota")
        recovered = self.service.collect("g")
        self.assertFalse(recovered.complete)
        self.assertEqual(recovered.missing, ("ph",))
        self.assertTrue(recovered.component("quota").present)

        # Repeating with ph still down changes nothing, adds no audit.
        audit_count = self.server.runtime.auditor.count()
        same = self.service.collect("g")
        self.assertEqual(same, recovered)
        self.assertEqual(self.server.runtime.auditor.count(), audit_count)

    def test_force_starts_a_new_pass(self) -> None:
        first = self.service.collect("h")
        second = self.service.collect("h", force=True)
        self.assertIsNot(first, second)
        self.assertNotEqual(first.summary_id, second.summary_id)

    def test_summary_is_audited_with_structured_detail(self) -> None:
        status = self.service.collect("i")
        last = self.server.runtime.auditor.last()
        self.assertIsNotNone(last)
        assert last is not None
        self.assertEqual(last.kind, "plant-status")
        detail = json.loads(last.detail)
        self.assertEqual(detail["summary_id"], status.summary_id)
        self.assertEqual(detail["complete"], True)
        self.assertEqual(detail["missing"], [])
        self.assertEqual(detail["fingerprint"], status.fingerprint)
        self.assertEqual(last.id, status.audit_id)

    def test_status_route_formats_and_header_key(self) -> None:
        status_code, envelope = self.call("/status", key="j")
        self.assertEqual(status_code, 200)
        self.assertEqual(envelope["complete"], True)
        self.assertEqual(set(envelope["components"]), COMPONENTS)

        status_code, again = self.call("/status", key="j")
        self.assertEqual(again["summary_id"], envelope["summary_id"])

        status_code, text = self.call("/status?format=text", key="j")
        self.assertEqual(status_code, 200)
        self.assertIn("waterplant control report", text)

    def test_legacy_endpoints_still_serve_their_shapes(self) -> None:
        status_code, snapshot = self.call("/snapshot")
        self.assertEqual(status_code, 200)
        self.assertEqual(set(snapshot), COMPONENTS)
        status_code, describe = self.call("/describe")
        self.assertEqual(status_code, 200)
        self.assertTrue(all(isinstance(line, str) for line in describe.values()))
        status_code, report = self.call("/report")
        self.assertEqual(status_code, 200)
        self.assertIn("waterplant control report", report)

    def test_missing_reader_injection_rejects_unknown_component(self) -> None:
        with self.assertRaises(KeyError):
            self.service.fail_reader("ghost")


if __name__ == "__main__":
    unittest.main()
