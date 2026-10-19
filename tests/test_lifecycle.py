"""Generation races must never expose stale success as current."""

import unittest
from dataclasses import FrozenInstanceError, replace

from graphql_desk.lifecycle import Coordinator, Snapshot, Ticket


def snapshot() -> Snapshot:
    return Snapshot(
        project="file:///workspace",
        schema="main",
        source_uri="file:///workspace/query.py",
        source_version=1,
        dependency_versions=(("file:///workspace/fragment.py", 1),),
        schema_fingerprint="schema-1",
        config_fingerprint="config-1",
    )


class LifecycleTests(unittest.TestCase):
    def test_newer_request_rejects_older_completion_and_failure(self) -> None:
        coordinator = Coordinator[str]()
        original = snapshot()
        old = coordinator.begin(original)
        newer = replace(original, source_version=2)
        current = coordinator.begin(newer)
        self.assertFalse(coordinator.complete(old, original, "old"))
        self.assertFalse(coordinator.fail(old))
        self.assertFalse(coordinator.cancel(old))
        self.assertEqual(coordinator.state, "pending")
        self.assertIsNone(coordinator.value)
        self.assertTrue(coordinator.complete(current, newer, "new"))
        self.assertEqual(coordinator.state, "ready")
        self.assertEqual(coordinator.value, "new")
        self.assertFalse(coordinator.complete(current, newer, "duplicate"))
        self.assertFalse(coordinator.fail(old))
        self.assertFalse(coordinator.cancel(old))
        self.assertEqual(coordinator.state, "ready")
        self.assertEqual(coordinator.value, "new")

    def test_fresh_observation_checks_every_semantic_input(self) -> None:
        original = snapshot()
        changed = (
            replace(original, project="file:///other"),
            replace(original, schema="other"),
            replace(original, source_uri="file:///workspace/renamed.py"),
            replace(original, source_version=2),
            replace(original, dependency_versions=(("file:///fragment.py", 2),)),
            replace(original, schema_fingerprint="schema-2"),
            replace(original, config_fingerprint="config-2"),
        )
        for observed in changed:
            with self.subTest(observed=observed):
                coordinator = Coordinator[str]()
                ticket = coordinator.begin(original)
                self.assertFalse(coordinator.complete(ticket, observed, "stale"))
                self.assertEqual(coordinator.state, "unavailable")
                self.assertIsNone(coordinator.value)
                self.assertFalse(coordinator.complete(ticket, original, "retry"))

    def test_equal_snapshot_undo_and_cross_coordinator_tickets_are_distinct(
        self,
    ) -> None:
        coordinator = Coordinator[str]()
        original = snapshot()
        first = coordinator.begin(original)
        coordinator.begin(replace(original, source_version=2))
        restored = coordinator.begin(original)
        self.assertNotEqual(first, restored)
        self.assertFalse(coordinator.complete(first, original, "old"))
        other = Coordinator[str]().begin(original)
        self.assertFalse(coordinator.complete(other, original, "other"))
        self.assertFalse(coordinator.complete(Ticket(original), original, "forged"))
        self.assertTrue(coordinator.complete(restored, original, "restored"))

    def test_cancel_invalidate_failure_and_recovery_hide_old_success(self) -> None:
        for transition in ("cancel", "fail", "invalidate"):
            with self.subTest(transition=transition):
                coordinator = Coordinator[str]()
                original = snapshot()
                initial = coordinator.begin(original)
                self.assertTrue(coordinator.complete(initial, original, "initial"))
                current = coordinator.begin(original)
                self.assertIsNone(coordinator.value)
                if transition == "invalidate":
                    coordinator.invalidate()
                elif transition == "cancel":
                    self.assertTrue(coordinator.cancel(current))
                else:
                    self.assertTrue(coordinator.fail(current))
                self.assertEqual(coordinator.state, "unavailable")
                self.assertIsNone(coordinator.value)
                self.assertFalse(coordinator.complete(current, original, "late"))
                retry = coordinator.begin(original)
                self.assertTrue(coordinator.complete(retry, original, "recovered"))
                self.assertEqual(coordinator.value, "recovered")
                coordinator.invalidate()
                self.assertEqual(coordinator.state, "unavailable")
                self.assertIsNone(coordinator.value)

    def test_snapshot_and_ticket_cannot_be_rebound(self) -> None:
        original = snapshot()
        with self.assertRaises(FrozenInstanceError):
            setattr(original, "source_version", 2)
        ticket = Ticket(original)
        with self.assertRaises(FrozenInstanceError):
            setattr(ticket, "snapshot", replace(original, source_version=2))

    def test_dependencies_are_canonical_and_duplicates_rejected(self) -> None:
        original = snapshot()
        dependencies = (("file:///b.py", 2), ("file:///a.py", 1))
        normalized = replace(original, dependency_versions=dependencies)
        self.assertEqual(
            normalized,
            replace(original, dependency_versions=tuple(reversed(dependencies))),
        )
        with self.assertRaisesRegex(ValueError, "unique"):
            replace(
                original,
                dependency_versions=(("file:///a.py", 1), ("file:///a.py", 2)),
            )


if __name__ == "__main__":
    unittest.main()
