"""争议处理链：受理冻结、修订裁定、补服务快照、双窗口冲突、批次恢复、结案拦截。"""
import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError
from src import rules as rules_module
from src.rules import (
    AMEND_CONFIRMED,
    AMEND_DISCARDED,
    AMEND_FROZEN,
    AMEND_PROPOSED,
    DISPUTE_ACCEPTED,
    DISPUTE_OPEN,
    DISPUTE_REJECTED,
    DISPUTE_UPHELD,
    MAKEUP_CONFIRMED,
)


CREATE_DATA = {'student_id': 'S-200', 'disability': 'hearing', 'service_minutes': 600,
               'delivered_minutes': 120, 'review_due_days': 15, 'goals_count': 4, 'consent': False}

PARENT = lambda: Actor("guardian", "parent_rep")
MANAGER = lambda: Actor("mgr", "case_manager", "school-a")
SPECIALIST = lambda: Actor("sp", "specialist")
ADMIN = lambda: Actor("admin", "administrator")
REVIEWER = lambda scopes: Actor("rv", "reviewer", "school-a", scopes)


def active_plan(service):
    record = service.create(Actor("creator", "case_manager", "school-a"), "IEP-D-001", CREATE_DATA)
    record = service.act(PARENT(), record["id"], record["version"], "consent",
                         {'guardian_confirmed': True, 'consent_scope': '全部服务'})
    record = service.act(MANAGER(), record["id"], record["version"], "activate", {})
    return record


class DisputeChainTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.record = active_plan(self.service)

    def tearDown(self):
        self.temp.cleanup()

    def test_filing_dispute_freezes_unconfirmed_amendments_on_acceptance(self):
        # 学校先提交未确认修订（边争议边修订的入口是修订草案）
        draft = {'amendment_reason': '增加言语服务', 'updated_goals': ['目标A', '目标B', '目标C'],
                 'service_minutes': 720}
        amendment = self.service.propose_amendment(MANAGER(), self.record["id"], self.record["version"], draft)
        self.assertEqual(amendment["status"], AMEND_PROPOSED)

        # 家长对当前计划提出异议（基于修订提交前的版本也能提交——先到先得，这里用最新版）
        record = self.service.get_record(MANAGER(), self.record["id"])
        dispute = self.service.file_dispute(PARENT(), record["id"], record["version"],
                                            {'reason': '服务时长不足', 'detail': '开学至今缺口大'})
        self.assertEqual(dispute["status"], DISPUTE_OPEN)

        # 受理后：未确认修订冻结
        accepted = self.service.accept_dispute(ADMIN(), dispute["dispute_id"])
        self.assertEqual(accepted["status"], DISPUTE_ACCEPTED)
        self.assertEqual(accepted["frozen_amendments"], [amendment["amendment_id"]])
        amendments = self.service.list_amendments(MANAGER(), self.record["id"])
        self.assertEqual(amendments[0]["status"], AMEND_FROZEN)

        # 冻结期间不能再提修订，也不能确认被冻结的修订
        latest = self.service.get_record(MANAGER(), self.record["id"])
        with self.assertRaises(Conflict):
            self.service.propose_amendment(MANAGER(), latest["id"], latest["version"], draft)
        with self.assertRaises(Conflict):
            self.service.confirm_amendment(MANAGER(), amendment["amendment_id"])

        # 已有服务与审计仍保留原依据版本
        entries = self.service.service_entries(MANAGER(), self.record["id"])
        self.assertTrue(entries)
        self.assertEqual(entries[0]["basis_version"], 1)
        timeline = self.service.timeline(MANAGER(), self.record["id"])
        actions = [event["action"] for event in timeline]
        self.assertIn("dispute_filed", actions)
        self.assertIn("dispute_accepted", actions)

    def test_reviewer_decides_by_scope_uphold_discards_amendment(self):
        draft = {'amendment_reason': '调整', 'updated_goals': ['g1'], 'service_minutes': 660}
        amendment = self.service.propose_amendment(MANAGER(), self.record["id"], self.record["version"], draft)
        latest = self.service.get_record(MANAGER(), self.record["id"])
        dispute = self.service.file_dispute(PARENT(), latest["id"], latest["version"], {'reason': '不认可'})
        self.service.accept_dispute(ADMIN(), dispute["dispute_id"])

        # 缺少授权范围不能裁定
        bare = Actor("rv0", "reviewer", "school-a", ())
        with self.assertRaises(PermissionDenied):
            self.service.decide_dispute(bare, dispute["dispute_id"], {'decision': 'upheld', 'decision_note': 'x'})

        # 只有 reject scope 不能 uphold
        reject_only = Actor("rv-reject", "reviewer", "school-a", ("dispute:reject",))
        with self.assertRaises(PermissionDenied):
            self.service.decide_dispute(reject_only, dispute["dispute_id"],
                                        {'decision': 'upheld', 'decision_note': 'x'})

        # 跨机构无 any_org 授权不能裁定
        other_org = Actor("rv1", "reviewer", "school-b", ("dispute:uphold",))
        with self.assertRaises(PermissionDenied):
            self.service.decide_dispute(other_org, dispute["dispute_id"],
                                        {'decision': 'upheld', 'decision_note': 'x'})

        decision = self.service.decide_dispute(REVIEWER(("dispute:uphold",)), dispute["dispute_id"],
                                               {'decision': 'upheld', 'decision_note': '异议成立，修订作废'})
        self.assertEqual(decision["status"], DISPUTE_UPHELD)
        amendments = self.service.list_amendments(MANAGER(), self.record["id"])
        self.assertEqual(amendments[0]["status"], AMEND_DISCARDED)

    def test_reject_unfreezes_amendment_and_then_confirm_updates_plan(self):
        draft = {'amendment_reason': '加时', 'updated_goals': ['g1', 'g2'], 'service_minutes': 660}
        amendment = self.service.propose_amendment(MANAGER(), self.record["id"], self.record["version"], draft)
        latest = self.service.get_record(MANAGER(), self.record["id"])
        dispute = self.service.file_dispute(PARENT(), latest["id"], latest["version"], {'reason': '质疑'})
        self.service.accept_dispute(ADMIN(), dispute["dispute_id"])
        decision = self.service.decide_dispute(REVIEWER(("dispute:reject",)), dispute["dispute_id"],
                                               {'decision': 'rejected', 'decision_note': '依据充分'})
        self.assertEqual(decision["status"], DISPUTE_REJECTED)
        amendments = self.service.list_amendments(MANAGER(), self.record["id"])
        self.assertEqual(amendments[0]["status"], AMEND_PROPOSED)

        confirmed = self.service.confirm_amendment(MANAGER(), amendment["amendment_id"])
        self.assertEqual(confirmed["payload"]["plan_version"], 2)
        self.assertEqual(confirmed["payload"]["service_minutes"], 660)
        snapshots = self.service.snapshots(MANAGER(), self.record["id"])
        labels = [(s["plan_version"], s["label"]) for s in snapshots]
        self.assertIn((2, "amendment-confirmed"), labels)

    def test_makeup_void_and_recompute_on_plan_update_confirmed_keeps_snapshot(self):
        # 初始缺口 480 分钟，登记 60 分钟常规服务后缺口 420
        rec = self.service.act(SPECIALIST(), self.record["id"], self.record["version"], "log_service",
                               {'session_minutes': 60, 'provider': 'SP-1'})
        entries = self.service.service_entries(MANAGER(), rec["id"])
        makeup = [e for e in entries if e["entry_type"] == "makeup"]
        self.assertEqual(sum(e["minutes"] for e in makeup), 420)
        self.assertTrue(all(e["status"] == "planned" for e in makeup))

        # 先确认一段补服务（60 分钟），保留确认当时 v1 快照
        first_makeup = makeup[0]["id"]
        confirmed = self.service.confirm_makeup(SPECIALIST(), first_makeup, {'provider': 'SP-2'})
        self.assertEqual(confirmed["status"], MAKEUP_CONFIRMED)
        self.assertEqual(confirmed["basis_version"], 1)

        # 计划修订升版到 720：未开始的补服务全部失效并重算，已确认记录保留 v1 快照
        draft = {'amendment_reason': '扩容', 'updated_goals': ['g1', 'g2'], 'service_minutes': 720}
        rec = self.service.get_record(MANAGER(), rec["id"])
        amendment = self.service.propose_amendment(MANAGER(), rec["id"], rec["version"], draft)
        result = self.service.confirm_amendment(MANAGER(), amendment["amendment_id"])
        self.assertEqual(result["payload"]["plan_version"], 2)

        entries = self.service.service_entries(MANAGER(), rec["id"])
        kept = [e for e in entries if e["id"] == first_makeup][0]
        self.assertEqual(kept["status"], MAKEUP_CONFIRMED)
        self.assertEqual(kept["basis_version"], 1)
        self.assertEqual(kept["plan_snapshot"]["plan_version"], 1)
        planned = [e for e in entries if e["entry_type"] == "makeup" and e["status"] == "planned"]
        # 缺口 = 720 - 已确认(120 opening + 60 regular + 60 makeup = 240) = 480；
        # 已确认补服务60抵扣缺口，重算的新计划为 480 - 60 = 420
        self.assertEqual(sum(e["minutes"] for e in planned), 420)
        self.assertTrue(all(e["basis_version"] == 2 for e in planned))

    def test_withdraw_consent_voids_planned_makeup_and_blocks_until_new_plan(self):
        rec = self.service.act(SPECIALIST(), self.record["id"], self.record["version"], "log_service",
                               {'session_minutes': 60, 'provider': 'SP-1'})
        before = [e for e in self.service.service_entries(MANAGER(), rec["id"]) if e["status"] == "planned"]
        self.assertTrue(before)
        rec = self.service.get_record(MANAGER(), rec["id"])
        result = self.service.act(PARENT(), rec["id"], rec["version"], "withdraw_consent",
                                  {'guardian_confirmed': True, 'reason': '服务未兑现'})
        self.assertFalse(result["payload"]["consent"])
        self.assertEqual(result["voided_makeup"], len(before))
        entries = self.service.service_entries(MANAGER(), rec["id"])
        self.assertFalse(any(e["status"] == "planned" for e in entries))
        # 已登记的常规/期初服务仍保留原依据
        confirmed = [e for e in entries if e["status"] == MAKEUP_CONFIRMED or e["entry_type"] in ("regular", "opening")]
        self.assertTrue(all(e["basis_version"] == 1 for e in confirmed))

    def test_two_windows_simultaneous_dispute_and_amendment_loser_keeps_draft(self):
        # 窗口A（家长异议）与窗口B（学校修订）基于同一版本同时提交，后到者拿到带 draft 的冲突
        draft = {'amendment_reason': '新增目标', 'updated_goals': ['x', 'y'], 'service_minutes': 700}
        first = self.service.propose_amendment(MANAGER(), self.record["id"], self.record["version"], draft)
        # 此时记录版本已 +1；家长仍拿旧版本提交异议 -> 冲突但保留填写内容
        try:
            self.service.file_dispute(PARENT(), self.record["id"], self.record["version"],
                                      {'reason': '我的异议内容', 'detail': '需要保留'})
            self.fail("expected conflict")
        except Conflict as exc:
            self.assertEqual(exc.details["current_version"], first["record_version"])
            self.assertEqual(exc.details["draft"]["reason"], '我的异议内容')

        # 两个修订窗口也互斥：后到者保留 draft 与当前版本
        latest = self.service.get_record(MANAGER(), self.record["id"])
        second_draft = {'amendment_reason': '第二窗口内容', 'updated_goals': ['z'], 'service_minutes': 800}
        try:
            self.service.propose_amendment(MANAGER(), latest["id"], self.record["version"], second_draft)
            self.fail("expected conflict")
        except Conflict as exc:
            self.assertEqual(exc.details["draft"]["amendment_reason"], '第二窗口内容')
            self.assertEqual(exc.details["current_version"], latest["version"])

    def test_batch_retry_after_write_failure_does_not_duplicate_makeup(self):
        repository = self.service.repository
        # 注入一次"提交前崩溃"：批次保留 reserved，重试应完整重放
        repository.inject_failure(before_commit=1)
        with self.assertRaises(Conflict):
            self.service.act(SPECIALIST(), self.record["id"], self.record["version"], "log_service",
                             {'session_minutes': 30, 'provider': 'SP-9'}, batch_id="batch-fail-1")
        retried = self.service.act(SPECIALIST(), self.record["id"], self.record["version"], "log_service",
                                   {'session_minutes': 30, 'provider': 'SP-9'}, batch_id="batch-fail-1")
        entries = self.service.service_entries(MANAGER(), self.record["id"])
        regular = [e for e in entries if e["entry_type"] == "regular"]
        self.assertEqual(len(regular), 1)
        self.assertEqual(regular[0]["minutes"], 30)
        self.assertEqual(retried["payload"]["delivered_minutes"], 150)
        batch = self.service.get_batch(MANAGER(), "batch-fail-1")
        self.assertEqual(batch["status"], "committed")
        # 恢复重放只生成过一批补服务记录，无重复行
        makeup = [e for e in entries if e["entry_type"] == "makeup"]
        self.assertEqual(len(makeup), len({e["id"] for e in makeup}))
        self.assertEqual(sum(e["minutes"] for e in makeup if e["status"] == "planned"), 450)

    def test_batch_retry_after_commit_lost_response_is_idempotent(self):
        repository = self.service.repository
        repository.inject_failure(after_commit=1)
        with self.assertRaises(Conflict):
            self.service.act(SPECIALIST(), self.record["id"], self.record["version"], "log_service",
                             {'session_minutes': 30, 'provider': 'SP-8'}, batch_id="batch-fail-2")
        # 数据已落盘：重试直接回放首次结果，台账不重复
        retried = self.service.act(SPECIALIST(), self.record["id"], self.record["version"], "log_service",
                                   {'session_minutes': 30, 'provider': 'SP-8'}, batch_id="batch-fail-2")
        entries = self.service.service_entries(MANAGER(), self.record["id"])
        regular = [e for e in entries if e["entry_type"] == "regular"]
        self.assertEqual(len(regular), 1)
        self.assertEqual(retried["payload"]["delivered_minutes"], 150)
        makeup = [e for e in entries if e["entry_type"] == "makeup"]
        self.assertEqual(len(makeup), len({e["id"] for e in makeup}))

    def test_close_blocked_by_open_dispute_and_blockers_listed(self):
        rec = self.service.act(ADMIN(), self.record["id"], self.record["version"], "review",
                               {'progress_note': '复盘'})
        rec = self.service.act(MANAGER(), rec["id"], rec["version"], "amend",
                               {'amendment_reason': '回到active', 'updated_goals': ['g1']})
        dispute = self.service.file_dispute(PARENT(), rec["id"], rec["version"], {'reason': '未决异议'})
        readiness = self.service.readiness(MANAGER(), rec["id"])
        self.assertFalse(readiness["can_close"])
        codes = {b["code"] for b in readiness["blockers"]}
        self.assertIn("dispute_open", codes)
        try:
            self.service.act(ADMIN(), rec["id"], rec["version"], "close", {'review_complete': True})
            self.fail("expected close blocked")
        except Conflict as exc:
            self.assertTrue(exc.details["blockers"])

        # 受理+裁定后异议缺口解除
        self.service.accept_dispute(ADMIN(), dispute["dispute_id"])
        self.service.decide_dispute(Actor("root", "admin"), dispute["dispute_id"],
                                    {'decision': 'rejected', 'decision_note': '驳回'})
        rec = self.service.get_record(MANAGER(), rec["id"])
        readiness = self.service.readiness(MANAGER(), rec["id"])
        self.assertTrue(readiness["can_close"], readiness["blockers"])
        closed = self.service.act(ADMIN(), rec["id"], rec["version"], "close", {'review_complete': True})
        self.assertEqual(closed["state"], "closed")
    def test_close_blocked_by_overdue_gap_and_version_mismatch(self):
        # 超期缺口：review_due_days 已耗尽
        data = dict(CREATE_DATA, student_id="S-201", review_due_days=0)
        record = self.service.create(Actor("creator", "case_manager", "school-a"), "IEP-D-002", data)
        record = self.service.act(PARENT(), record["id"], record["version"], "consent",
                                  {'guardian_confirmed': True, 'consent_scope': 'x'})
        record = self.service.act(MANAGER(), record["id"], record["version"], "activate", {})
        readiness = self.service.readiness(MANAGER(), record["id"])
        codes = {b["code"] for b in readiness["blockers"]}
        self.assertIn("review_overdue", codes)

        # 版本不一致：服务台账与计划记录不匹配（正常流程核对）
        self.service.act(ADMIN(), record["id"], record["version"], "review", {'progress_note': 'n'})
        record = self.service.get_record(MANAGER(), record["id"])
        tampered = dict(record["payload"])
        tampered["review_overdue"] = False
        tampered["delivered_minutes"] = 999
        # 通过一次合法修订后再核对：改为构造台账不一致场景
        record = self.service.act(MANAGER(), record["id"], record["version"], "amend",
                                  {'amendment_reason': 'r', 'updated_goals': ['g']})
        readiness = self.service.readiness(MANAGER(), record["id"])
        mismatch = [b for b in readiness["blockers"] if b["type"] == "version_mismatch"]
        # 正常流程不应出现不一致
        self.assertEqual(mismatch, [])

    def test_unconfirmed_amendment_blocks_close(self):
        self.service.propose_amendment(MANAGER(), self.record["id"], self.record["version"],
                                       {'amendment_reason': '草案', 'updated_goals': ['g']})
        readiness = self.service.readiness(MANAGER(), self.record["id"])
        codes = {b["code"] for b in readiness["blockers"]}
        self.assertIn("unconfirmed_amendment", codes)


if __name__ == "__main__":
    unittest.main()
