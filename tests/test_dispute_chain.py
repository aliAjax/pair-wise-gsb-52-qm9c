import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, NotFound, PermissionDenied, ValidationError


PARENT = Actor("parent", "parent_rep")
MANAGER = Actor("manager", "case_manager")
ADMIN = Actor("principal", "administrator")
OFFICER = Actor("officer", "review_officer")
SPECIALIST = Actor("sp", "specialist")

DATA = {
    'student_id': 'S-200', 'disability': 'hearing', 'service_minutes': 600,
    'delivered_minutes': 0, 'review_due_days': -5, 'goals_count': 2, 'consent': False,
}


class DisputeChainTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.svc = build_service(str(Path(self.temp.name) / "test.db"))
        self.record = self.svc.create(MANAGER, "IEP-D-1", DATA)
        self.rid = self.record["id"]

    def tearDown(self):
        self.temp.cleanup()

    def _activate(self):
        self.record = self.svc.act(PARENT, self.rid, self.record["version"], "consent",
                                   {"guardian_confirmed": True, "consent_scope": "语言训练"})
        self.record = self.svc.act(MANAGER, self.rid, self.record["version"], "activate", {})
        return self.record

    def test_freeze_amendments_on_acceptance_and_keep_service_basis(self):
        rec = self._activate()
        # 生效即出现服务缺口，生成一条未开始补服务（依据第1版）。
        rows = self.svc.list_service_rows(ADMIN, self.rid)
        pending = [r for r in rows if r["status"] == "pending"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["basis_revision"], 1)

        # 争议受理前，学校先提一个修订草案；受理后该草案被冻结。
        rec = self.svc.act(MANAGER, self.rid, rec["version"], "propose_amendment",
                           {"amendment_reason": "加目标", "updated_goals": ["目标X", "目标Y", "目标Z"]})
        amendment_id = self.svc.list_amendments(ADMIN, self.rid)[0]["id"]

        rec = self.svc.act(PARENT, self.rid, rec["version"], "file_dispute",
                           {"topic": "amendment", "reason": "修订没跟我商量"})
        self.assertEqual(rec["state"], "disputed")
        amendment = self.svc.list_amendments(ADMIN, self.rid)[0]
        self.assertEqual(amendment["status"], "frozen")

        # 争议期间不能提交/确认修订（状态机直接拦住）。
        with self.assertRaises(Conflict):
            self.svc.act(MANAGER, self.rid, rec["version"], "propose_amendment",
                         {"amendment_reason": "x", "updated_goals": ["a"]})
        with self.assertRaises(Conflict):
            self.svc.act(MANAGER, self.rid, rec["version"], "confirm_amendment",
                         {"amendment_id": amendment_id})

        # 已有服务在争议期间照常登记，并保留当时依据快照（仍是第1版）。
        rec = self.svc.act(SPECIALIST, self.rid, rec["version"], "log_service",
                           {"session_minutes": 60, "provider": "SP-9"})
        self.assertEqual(rec["state"], "disputed")
        service_rows = self.svc.list_service_rows(ADMIN, self.rid)
        logged = [r for r in service_rows if r["kind"] == "service"][-1]
        self.assertEqual(logged["basis_revision"], 1)
        self.assertEqual(logged["basis_snapshot"]["plan_revision"], 1)

        # 审计里的服务依据始终指向旧版本。
        timeline = self.svc.timeline(ADMIN, self.rid)
        self.assertEqual([e["action"] for e in timeline].count("log_service"), 1)

        # 复核人授权范围：review_officer 不能裁定 amendment 类异议。
        with self.assertRaises(PermissionDenied):
            self.svc.act(OFFICER, self.rid, rec["version"], "resolve_dispute",
                         {"dispute_id": self.svc.list_disputes(ADMIN, self.rid)[0]["id"],
                          "outcome": "uphold_plan", "note": "超出授权"})
        # administrator 在授权范围内裁定，冻结的修订恢复为草案。
        rec = self.svc.act(ADMIN, self.rid, rec["version"], "resolve_dispute",
                           {"dispute_id": self.svc.list_disputes(ADMIN, self.rid)[0]["id"],
                            "outcome": "uphold_plan", "note": "维持现计划"})
        self.assertEqual(rec["state"], "active")
        self.assertEqual(self.svc.list_amendments(ADMIN, self.rid)[0]["status"], "proposed")

    def test_amendment_confirmation_voids_unstarted_makeup_and_recalculates(self):
        rec = self._activate()
        pending_before = [r for r in self.svc.list_service_rows(ADMIN, self.rid) if r["status"] == "pending"]
        self.assertEqual(len(pending_before), 1)
        old_id = pending_before[0]["id"]

        # 登记一部分服务，未开始补服务按剩余缺口重算分钟数（不新增行）。
        rec = self.svc.act(SPECIALIST, self.rid, rec["version"], "log_service",
                           {"session_minutes": 100, "provider": "SP-1"})
        pending = [r for r in self.svc.list_service_rows(ADMIN, self.rid) if r["status"] == "pending"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["id"], old_id)
        self.assertEqual(pending[0]["minutes"], 500)

        # 提交并确认修订：计划分钟数从600增到900，第2版。
        rec = self.svc.act(MANAGER, self.rid, rec["version"], "propose_amendment",
                           {"amendment_reason": "扩容", "updated_goals": ["g1", "g2"], "service_minutes": 900})
        amendment_id = self.svc.list_amendments(ADMIN, self.rid)[0]["id"]
        rec = self.svc.act(MANAGER, self.rid, rec["version"], "confirm_amendment",
                           {"amendment_id": amendment_id})
        self.assertEqual(rec["payload"]["plan_revision"], 2)
        rows = self.svc.list_service_rows(ADMIN, self.rid)
        # 旧版未开始补服务失效，按第2版重新生成。
        self.assertEqual(rec["payload"]["missing_minutes"], 800)
        old = next(r for r in rows if r["id"] == old_id)
        self.assertEqual(old["status"], "void")
        new_pending = [r for r in rows if r["status"] == "pending"]
        self.assertEqual(len(new_pending), 1)
        self.assertEqual(new_pending[0]["basis_revision"], 2)
        self.assertEqual(new_pending[0]["minutes"], 800)

        # 已确认的服务保留当时快照（第1版），不被重算。
        confirmed = [r for r in rows if r["kind"] == "service" and r["status"] == "confirmed"][-1]
        self.assertEqual(confirmed["basis_revision"], 1)

        # 完成补服务：确认后保留补服务当时快照（第2版），缺口清零且不再有待执行行。
        rec = self.svc.act(SPECIALIST, self.rid, rec["version"], "confirm_makeup",
                           {"service_id": new_pending[0]["id"], "confirmed_minutes": 800, "provider": "SP-2"})
        rows = self.svc.list_service_rows(ADMIN, self.rid)
        done = next(r for r in rows if r["id"] == new_pending[0]["id"])
        self.assertEqual(done["status"], "confirmed")
        self.assertEqual(done["basis_revision"], 2)
        self.assertEqual([r for r in rows if r["status"] == "pending"], [])
        self.assertEqual(rec["payload"]["missing_minutes"], 0)

    def test_consent_withdraw_voids_unstarted_makeup_and_reconsent(self):
        rec = self._activate()
        pending_id = [r["id"] for r in self.svc.list_service_rows(ADMIN, self.rid) if r["status"] == "pending"][0]
        rec = self.svc.act(PARENT, self.rid, rec["version"], "withdraw_consent", {"reason": "服务质量异议"})
        self.assertEqual(rec["state"], "consent_withdrawn")
        self.assertFalse(rec["payload"]["consent"])
        row = self.svc.get_record  # noqa
        pending = next(r for r in self.svc.list_service_rows(ADMIN, self.rid) if r["id"] == pending_id)
        self.assertEqual(pending["status"], "void")
        # 撤回后没有任何待执行补服务。
        self.assertEqual([r for r in self.svc.list_service_rows(ADMIN, self.rid) if r["status"] == "pending"], [])
        # 重新同意后回到 consented，激活后按当前计划重新生成补服务。
        rec = self.svc.act(PARENT, self.rid, rec["version"], "consent",
                           {"guardian_confirmed": True, "consent_scope": "语言训练"})
        self.assertEqual(rec["state"], "consented")
        rec = self.svc.act(MANAGER, self.rid, rec["version"], "activate", {})
        pending = [r for r in self.svc.list_service_rows(ADMIN, self.rid) if r["status"] == "pending"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["basis_revision"], 1)

    def test_dispute_with_concurrent_withdraw_consent_resolution(self):
        rec = self._activate()
        rec = self.svc.act(PARENT, self.rid, rec["version"], "file_dispute",
                           {"topic": "consent", "reason": "不同意继续"})
        dispute_id = self.svc.list_disputes(ADMIN, self.rid)[0]["id"]
        # 争议处理中家长同步撤回同意。
        rec = self.svc.act(PARENT, self.rid, rec["version"], "withdraw_consent", {"reason": "撤回"})
        self.assertEqual(rec["state"], "disputed_consent_withdrawn")
        # 此状态下复核只能按撤回同意裁定。
        with self.assertRaises(ValidationError):
            self.svc.act(ADMIN, self.rid, rec["version"], "resolve_dispute",
                         {"dispute_id": dispute_id, "outcome": "uphold_plan", "note": "x"})
        rec = self.svc.act(ADMIN, self.rid, rec["version"], "resolve_dispute",
                           {"dispute_id": dispute_id, "outcome": "withdraw_consent", "note": "确认撤回"})
        self.assertEqual(rec["state"], "consent_withdrawn")
        self.assertEqual(self.svc.list_disputes(ADMIN, self.rid)[0]["status"], "resolved")

    def test_concurrent_windows_later_writer_keeps_draft_and_sees_conflict(self):
        rec = self._activate()
        # 窗口A：学校修订先到。
        rec_a = self.svc.act(MANAGER, self.rid, rec["version"], "propose_amendment",
                             {"amendment_reason": "A窗口", "updated_goals": ["a1"]})
        # 窗口B：家长基于旧版本同时提异议，后到。
        with self.assertRaises(Conflict) as caught:
            self.svc.act(PARENT, self.rid, rec["version"], "file_dispute",
                         {"topic": "service_gap", "reason": "B窗口填好的异议内容"})
        details = caught.exception.details
        self.assertTrue(details["conflict"])
        self.assertEqual(details["current_version"], rec_a["version"])
        # 后到者填写的内容被保留，且能通过草稿接口取回。
        draft = self.svc.list_drafts(PARENT, self.rid, kind="file_dispute")[0]
        self.assertEqual(draft["payload"]["reason"], "B窗口填好的异议内容")
        self.assertEqual(draft["base_version"], rec["version"])
        self.assertEqual(draft["conflict_version"], rec_a["version"])

        # 两个修订窗口同时提交：后到者同样保留草稿。
        with self.assertRaises(Conflict) as caught2:
            self.svc.act(MANAGER, self.rid, rec["version"], "propose_amendment",
                         {"amendment_reason": "B窗口修订", "updated_goals": ["b1"]})
        draft2 = self.svc.list_drafts(MANAGER, self.rid, kind="propose_amendment")[0]
        self.assertEqual(draft2["payload"]["amendment_reason"], "B窗口修订")

    def test_batch_retry_is_idempotent_and_replays_without_duplicate_makeup(self):
        rec = self._activate()
        original_pending = [r for r in self.svc.list_service_rows(ADMIN, self.rid) if r["status"] == "pending"]
        self.assertEqual(len(original_pending), 1)
        v = rec["version"]
        ops = [
            {"action": "log_service", "data": {"session_minutes": 30, "provider": "SP-1"}},
            {"action": "log_service", "data": {"session_minutes": 20, "provider": "SP-1"}},
        ]
        first = self.svc.run_batch(SPECIALIST, self.rid, v, "batch-1", ops)
        self.assertFalse(first["replayed"])
        self.assertEqual(first["results"]["items"][-1]["record_version"], v + 2)
        # 用同一个完整批次重试：直接回放，不重复执行，补服务记录不重复生成。
        second = self.svc.run_batch(SPECIALIST, self.rid, v, "batch-1", ops)
        self.assertTrue(second["replayed"])
        rec_now = self.svc.get_record(ADMIN, self.rid)
        self.assertEqual(rec_now["version"], v + 2)
        rows = self.svc.list_service_rows(ADMIN, self.rid)
        self.assertEqual(len([r for r in rows if r["kind"] == "service"]), 2)
        pending = [r for r in rows if r["status"] == "pending"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["minutes"], 550)

        # 批次整体是原子的：第二个动作非法输入时，第一个动作也不落库。
        bad_ops = [
            {"action": "log_service", "data": {"session_minutes": 10, "provider": "SP-1"}},
            {"action": "confirm_makeup", "data": {"service_id": 999999}},
        ]
        with self.assertRaises(NotFound):
            self.svc.run_batch(SPECIALIST, self.rid, rec_now["version"], "batch-2", bad_ops)
        self.assertEqual(self.svc.get_record(ADMIN, self.rid)["version"], v + 2)
        # 同一批次键从完整批次恢复：清理残留后整体重试成功。
        recovered = self.svc.run_batch(SPECIALIST, self.rid, rec_now["version"], "batch-2",
                                       bad_ops[:1] + [
                                           {"action": "confirm_makeup",
                                            "data": {"service_id": pending[0]["id"],
                                                     "provider": "SP-2"}}])
        self.assertFalse(recovered["replayed"])
        self.assertEqual(self.svc.get_record(ADMIN, self.rid)["payload"]["missing_minutes"], 0)

    def test_close_blocked_by_open_dispute_overdue_gap_and_lists_gaps(self):
        rec = self._activate()
        # 超期 + 600分钟缺口 + 未决异议，三重拦截。
        rec = self.svc.act(PARENT, self.rid, rec["version"], "file_dispute",
                           {"topic": "service_gap", "reason": "缺口未补"})
        with self.assertRaises(Conflict) as caught:
            self.svc.act(ADMIN, self.rid, rec["version"], "close", {"review_complete": True})
        kinds = {gap["kind"] for gap in caught.exception.details["gaps"]}
        self.assertIn("open_dispute", kinds)
        self.assertIn("overdue_service_gap", kinds)
        # 只读缺口接口返回同样的详情。
        gaps = self.svc.gaps(ADMIN, self.rid)
        self.assertTrue(any(g["kind"] == "open_dispute" for g in gaps))

        # 裁定异议后，未决异议缺口消失；超期缺口仍在，继续拦截。
        dispute_id = self.svc.list_disputes(ADMIN, self.rid)[0]["id"]
        rec = self.svc.act(ADMIN, self.rid, rec["version"], "resolve_dispute",
                           {"dispute_id": dispute_id, "outcome": "uphold_plan", "note": "维持"})
        gaps = self.svc.gaps(ADMIN, self.rid)
        self.assertEqual({g["kind"] for g in gaps}, {"overdue_service_gap"})
        with self.assertRaises(Conflict):
            self.svc.act(ADMIN, self.rid, rec["version"], "close", {"review_complete": True})

        # 先补齐全部缺口（解除超期缺口），再复查解除超期标记，之后允许结案。
        pending = [r for r in self.svc.list_service_rows(ADMIN, self.rid) if r["status"] == "pending"][0]
        rec = self.svc.act(SPECIALIST, self.rid, rec["version"], "confirm_makeup",
                           {"service_id": pending["id"], "provider": "SP"})
        rec = self.svc.act(ADMIN, self.rid, rec["version"], "review", {"progress_note": "复核"})
        rec = self.svc.act(ADMIN, self.rid, rec["version"], "close", {"review_complete": True})
        self.assertEqual(rec["state"], "closed")

    def test_revision_mismatch_gap_detected(self):
        rec = self._activate()
        # 直接制造版本不一致：一条未开始补服务依据的修订号与当前确认版本不同。
        rec = self.svc.act(MANAGER, self.rid, rec["version"], "propose_amendment",
                           {"amendment_reason": "v2", "updated_goals": ["x"]})
        amendment_id = self.svc.list_amendments(ADMIN, self.rid)[0]["id"]
        rec = self.svc.act(MANAGER, self.rid, rec["version"], "confirm_amendment",
                           {"amendment_id": amendment_id})
        # 当前为第2版；手工把待执行补服务改回第1版依据，制造不一致。
        import sqlite3
        with self.svc.repository._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE service_rows SET basis_revision=1 WHERE record_id=? AND status='pending'", (self.rid,))
            conn.commit()
        gaps = self.svc.gaps(ADMIN, self.rid)
        mismatch = [g for g in gaps if g["kind"] == "revision_mismatch"]
        self.assertEqual(len(mismatch), 1)
        self.assertEqual(mismatch[0]["basis_revision"], 1)
        self.assertEqual(mismatch[0]["current_revision"], 2)
