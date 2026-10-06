import unittest

from src.domain import Actor, Conflict, NotFound, PermissionDenied, ValidationError
from src.rules import DomainRules


def gap_plan(service_minutes=600, delivered=0, review_due_days=-1, goals=2):
    return {
        'student_id': 'S-1', 'disability': 'hearing', 'service_minutes': service_minutes,
        'delivered_minutes': delivered, 'review_due_days': review_due_days,
        'goals_count': goals, 'consent': False,
    }


class GapRulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = DomainRules()

    def test_review_scope_limits_topics(self):
        self.assertTrue(self.rules.can_review_topic('review_officer', 'service_gap'))
        self.assertTrue(self.rules.can_review_topic('review_officer', 'overdue'))
        self.assertFalse(self.rules.can_review_topic('review_officer', 'amendment'))
        self.assertFalse(self.rules.can_review_topic('review_officer', 'consent'))
        self.assertTrue(self.rules.can_review_topic('administrator', 'amendment'))
        self.assertTrue(self.rules.can_review_topic('administrator', 'consent'))

    def test_open_gaps_list_all(self):
        record = {'id': 1, 'state': 'active', 'payload': self.rules.prepare_create(gap_plan())}
        gaps = self.rules.open_gaps(
            record,
            [{'id': 7, 'topic': 'service_gap', 'status': 'open'}],
            current_revision=1,
            stale_service_rows=[{'id': 9, 'basis_revision': 3}],
        )
        kinds = {gap['kind'] for gap in gaps}
        self.assertEqual(kinds, {'open_dispute', 'overdue_service_gap', 'revision_mismatch'})
        mismatch = next(gap for gap in gaps if gap['kind'] == 'revision_mismatch')
        self.assertEqual(mismatch['service_id'], 9)
        self.assertEqual(mismatch['current_revision'], 1)

    def test_overdue_gap_requires_both_overdue_and_missing(self):
        on_track = {'id': 1, 'state': 'active', 'payload': self.rules.prepare_create(gap_plan(delivered=600))}
        self.assertEqual(self.rules.open_gaps(on_track, [], 1, []), [])
        no_gap_but_overdue = {'id': 1, 'state': 'active', 'payload': self.rules.prepare_create(gap_plan(delivered=600))}
        self.assertEqual(self.rules.open_gaps(no_gap_but_overdue, [], 1, []), [])

    def test_snapshot_is_version_basis(self):
        payload = self.rules.prepare_create(gap_plan())
        snapshot = self.rules.plan_snapshot({'version': 3, 'payload': payload})
        self.assertEqual(snapshot['plan_revision'], 1)
        self.assertEqual(snapshot['plan_version'], 3)
        self.assertEqual(snapshot['service_minutes'], 600)

    def test_legacy_amend_bumps_revision(self):
        prepared = self.rules.prepare_create(gap_plan(delivered=100))
        record = {'id': 1, 'state': 'active', 'version': 4, 'payload': prepared}
        state, payload, summary = self.rules.apply_action(
            record, 'amend', {'amendment_reason': 'r', 'updated_goals': ['g1', 'g2', 'g3']}
        )
        self.assertEqual(state, 'active')
        self.assertEqual(payload['plan_revision'], 2)
        self.assertEqual(payload['missing_minutes'], 500)
