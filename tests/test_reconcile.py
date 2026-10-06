"""补服务重算规则：失效/重算/快照保留的纯规则测试。"""
import unittest

from src.rules import DomainRules


class ReconcileRulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = DomainRules()

    def _payload(self, service=600, delivered=120, plan_version=1, consent=True):
        return {"service_minutes": service, "delivered_minutes": delivered,
                "plan_version": plan_version, "consent": consent}

    def _entry(self, eid, status, minutes=60, entry_type="makeup"):
        return {"id": eid, "status": status, "minutes": minutes, "entry_type": entry_type,
                "basis_version": 1, "plan_snapshot": {"plan_version": 1}}

    def test_initial_gap_planned_in_chunks(self):
        actions = self.rules.reconcile_makeup([], self._payload(delivered=120))
        created = [a for a in actions if a["action"] == "create"]
        self.assertEqual(sum(a["minutes"] for a in created), 480)
        self.assertTrue(all(a["minutes"] <= 60 for a in created))

    def test_plan_update_voids_planned_and_keeps_confirmed_offset(self):
        existing = [self._entry(1, "confirmed"), self._entry(2, "planned"), self._entry(3, "planned")]
        # 计划升到 720，已履约：opening/regular 合计在 delivered 内，另已确认补服务60
        payload = self._payload(service=720, delivered=180, plan_version=2)
        # delivered=180 表示 opening+regular；已确认 makeup 60 额外抵扣
        actions = self.rules.reconcile_makeup(existing, payload)
        self.assertEqual([a for a in actions if a["action"] == "void"], [{"id": 2, "action": "void"}, {"id": 3, "action": "void"}])
        created = [a for a in actions if a["action"] == "create"]
        # gap = 720-180 = 540；再减已确认补服务60 => 480
        self.assertEqual(sum(a["minutes"] for a in created), 480)
        self.assertTrue(all(a["minutes"] <= 60 for a in created))

    def test_consent_withdrawn_voids_without_recreate(self):
        existing = [self._entry(2, "planned"), self._entry(3, "planned"), self._entry(1, "confirmed")]
        actions = self.rules.reconcile_makeup(existing, self._payload(delivered=120, consent=False),
                                              consent_active=False)
        self.assertTrue(all(a["action"] == "void" for a in actions))
        self.assertEqual({a["id"] for a in actions}, {2, 3})

    def test_confirmed_makeup_kept_when_gap_zero(self):
        existing = [self._entry(1, "confirmed")]
        actions = self.rules.reconcile_makeup(existing, self._payload(delivered=600))
        self.assertEqual(actions, [])


if __name__ == "__main__":
    unittest.main()
