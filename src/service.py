"""业务用例编排：争议处理链、权限检查、乐观并发、补服务对账与审计。"""
import json
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, NotFound, PermissionDenied, ValidationError, integer, text
from .repository import Repository
from .rules import DomainRules

# 这些窗口允许在版本冲突时保留填写内容（家长异议窗口 / 学校修订窗口等）。
DRAFT_ACTIONS = {
    'file_dispute', 'propose_amendment', 'confirm_amendment',
    'withdraw_consent', 'log_service', 'confirm_makeup', 'review',
}
LEGACY_ACTIONS = {'consent', 'activate', 'log_service', 'review', 'amend', 'close'}
MAKEUP_ELIGIBLE_STATES = {'active', 'under_review', 'disputed'}


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    # -- 身份与权限 ------------------------------------------------------

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def _authorize(self, actor: Actor, action: str) -> None:
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行%s" % action)

    # -- 创建与查询 ------------------------------------------------------

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        seeds: List[Dict[str, Any]] = []
        if int(prepared["delivered_minutes"]) > 0:
            seeds.append({
                "kind": "initial",
                "minutes": int(prepared["delivered_minutes"]),
                "provider": actor.user_id,
                "basis_revision": prepared["plan_revision"],
                "basis_snapshot": self.rules.plan_snapshot({"version": 1, "payload": prepared}),
            })
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id, seed_services=seeds)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()

    # -- 争议链明细 ------------------------------------------------------

    def list_disputes(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self.repository.get(record_id)
        return self.repository.list_disputes(record_id)

    def list_amendments(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self.repository.get(record_id)
        return self.repository.list_amendments(record_id)

    def list_service_rows(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self.repository.get(record_id)
        return self.repository.list_service_rows(record_id)

    def list_drafts(self, actor: Actor, record_id: int, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self.repository.get(record_id)
        return self.repository.list_drafts(record_id, actor_id=actor.user_id, kind=kind)

    def gaps(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self._gaps(self.repository.get(record_id))

    # -- 单动作入口：版本冲突时为后到者保留填写内容 ----------------------

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        self._authorize(actor, action)
        int(expected_version)
        data = dict(data or {})
        try:
            with self.repository.workspace(record_id, int(expected_version), actor.user_id) as ws:
                self._apply(ws, action, data, actor, batch_key="")
                result = ws.record
            return result
        except Conflict as exc:
            if exc.details.get("current_version") and action in DRAFT_ACTIONS:
                current_version = int(exc.details["current_version"])
                draft = self.repository.save_draft(
                    record_id, actor.user_id, action, data, int(expected_version), current_version
                )
                exc.details["draft"] = draft
                exc.details["conflict"] = True
            raise

    # -- 批次入口：写入失败后从完整批次恢复，重试不重复生成补服务 --------

    def run_batch(
        self,
        actor: Actor,
        record_id: int,
        expected_version: int,
        batch_key: str,
        operations: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        batch_key = text({"batch_key": batch_key}, "batch_key")
        if isinstance(expected_version, bool) or not isinstance(expected_version, int):
            raise ValidationError("expected_version必须是整数")
        if not isinstance(operations, list) or not operations:
            raise ValidationError("operations必须是非空列表")
        if len(operations) > 200:
            raise ValidationError("单批次不能超过200个动作")
        for item in operations:
            if not isinstance(item, dict) or not isinstance(item.get("action"), str) or not item["action"].strip():
                raise ValidationError("每个批次项必须包含action")
            self._authorize(actor, item["action"])
        record = self.repository.get(record_id)
        # 已完成批次直接回放结果，绝不重复执行（补服务记录不会重复生成）。
        existing = self.repository.get_batch(batch_key)
        if existing is not None and existing["record_id"] != record_id:
            raise ValidationError("批次键属于其他记录")
        if existing is not None and existing["status"] == "done":
            return {"replayed": True, "batch_key": batch_key, "operations": existing["operations"], "results": existing["result"]}
        self.repository.start_batch(batch_key, record_id, len(operations), actor.user_id)
        results: List[Dict[str, Any]] = []
        with self.repository.batch_workspace(batch_key, record_id, int(expected_version), actor.user_id) as ws:
            # 恢复“运行中”批次：清理上次失败可能残留的未确认行（已确认行随事务回滚不存在）。
            self.repository.delete_batch_service_rows_conn(ws.connection, batch_key)
            for index, item in enumerate(operations):
                applied = self._apply(ws, item["action"], dict(item.get("data") or {}), actor, batch_key=batch_key)
                results.append({
                    "index": index,
                    "action": item["action"],
                    "record_version": ws.record["version"],
                    "summary": applied.get("summary", ""),
                    "ids": applied.get("ids", {}),
                })
            payload = {
                "record_version": ws.record["version"],
                "state": ws.record["state"],
                "items": results,
            }
            self.repository.complete_batch(ws.connection, batch_key, payload, ws.now)
        return {"replayed": False, "batch_key": batch_key, "operations": len(operations), "results": payload}

    # -- 核心执行（可在单动作事务或批次事务内复用） ----------------------

    def _apply(self, ws, action: str, data: Dict[str, Any], actor: Actor, batch_key: str) -> Dict[str, Any]:
        repo = self.repository
        conn = ws.connection
        record = ws.record
        # 结案缺口检查先于状态转换：争议/超期/版本不一致都要拦住并列出详情。
        if action == "close":
            gaps = self._gaps(record)
            if gaps:
                raise Conflict("存在未结缺口，不能结案", {"gaps": gaps})
        new_state = self.rules.require_transition(record, action)
        ids: Dict[str, int] = {}

        if action in LEGACY_ACTIONS:
            new_state, new_payload, summary = self.rules.apply_action(record, action, data)
            if action == "log_service":
                snapshot = self.rules.plan_snapshot({"version": ws.next_version, "payload": new_payload})
                service_id = repo.insert_service_row(conn, record["id"], {
                    "kind": "service",
                    "minutes": self.rules.validate_service_log(data),
                    "provider": text(data, "provider"),
                    "status": "confirmed",
                    "basis_revision": snapshot["plan_revision"],
                    "basis_snapshot": snapshot,
                    "batch_key": batch_key,
                }, ws.now)
                ids["service_id"] = service_id
                total = repo.confirmed_minutes_conn(conn, record["id"])
                new_payload = self.rules.refresh_totals(new_payload, total)
            ws.apply_record(new_state, new_payload, action, {
                "summary": summary, "input": data, "from": record["state"], "to": new_state,
                "ids": ids, "batch_key": batch_key,
            })
            if action == "log_service":
                self._reconcile_makeup(ws, "服务已登记，缺口重算", batch_key)
            elif action in ("activate", "amend"):
                self._reconcile_makeup(ws, "计划已更新" if action == "amend" else "计划生效", batch_key)
            return {"summary": summary, "ids": ids}

        if action == "file_dispute":
            parsed = self.rules.validate_dispute(data)
            parsed["basis_revision"] = int(record["payload"].get("plan_revision", 1))
            dispute_id = repo.insert_dispute(conn, record["id"], parsed, actor.user_id, ws.now)
            ids["dispute_id"] = dispute_id
            repo.set_amendments_status_conn(conn, record["id"], "proposed", "frozen")
            ws.apply_record("disputed", dict(record["payload"]), action, {
                "summary": "家长异议已受理，未确认修订已冻结",
                "input": data, "from": record["state"], "to": "disputed",
                "ids": ids, "topic": parsed["topic"], "basis_revision": parsed["basis_revision"],
            })
            return {"summary": "异议已受理", "ids": ids}

        if action == "resolve_dispute":
            dispute_id = integer(data, "dispute_id")
            row = repo.get_dispute_conn(conn, record["id"], dispute_id)
            if row is None:
                raise NotFound("异议不存在")
            if row["status"] != "open":
                raise Conflict("该异议已裁定")
            if not self.rules.can_review_topic(actor.role, row["topic"]):
                raise PermissionDenied("复核人授权范围不包含%s类争议" % row["topic"])
            parsed = self.rules.validate_resolution(data, actor.role)
            p = dict(record["payload"])
            if record["state"] == "disputed_consent_withdrawn" and parsed["outcome"] != "withdraw_consent":
                raise ValidationError("同意已撤回，只能按撤回同意裁定")
            if parsed["outcome"] == "withdraw_consent":
                new_state = "consent_withdrawn"
                p["consent"] = False
                p["plan_status"] = "consent_withdrawn"
                repo.set_amendments_status_conn(conn, record["id"], "frozen", "rejected")
                void_reason = "同意撤回，未开始补服务失效"
            else:
                new_state = "active"
                p["plan_status"] = "active"
                # 争议结束后解冻未确认修订，恢复为可处理的草案。
                repo.set_amendments_status_conn(conn, record["id"], "frozen", "proposed")
                void_reason = ""
            repo.resolve_dispute(conn, dispute_id, parsed, actor.user_id, ws.now)
            ws.apply_record(new_state, p, action, {
                "summary": "异议已按授权范围裁定：%s" % parsed["outcome"],
                "input": data, "from": record["state"], "to": new_state,
                "ids": {"dispute_id": dispute_id}, "outcome": parsed["outcome"],
            })
            if new_state == "consent_withdrawn":
                repo.void_pending_makeup_conn(conn, record["id"], void_reason, ws.now)
            return {"summary": "异议已裁定", "ids": {"dispute_id": dispute_id}}

        if action == "withdraw_consent":
            parsed = self.rules.validate_withdraw(data)
            p = dict(record["payload"])
            p["consent"] = False
            p["plan_status"] = "consent_withdrawn"
            ws.apply_record(new_state, p, action, {
                "summary": "监护人撤回同意，未开始的补服务失效",
                "input": data, "from": record["state"], "to": new_state,
            })
            repo.void_pending_makeup_conn(conn, record["id"], "同意撤回，未开始补服务失效", ws.now)
            if record["state"] == "active":
                repo.set_amendments_status_conn(conn, record["id"], "proposed", "rejected")
            return {"summary": "同意已撤回", "ids": ids}

        if action == "propose_amendment":
            fields = self.rules.validate_amendment(data)
            prior_rows = repo.amendments_conn(conn, record["id"])
            max_revision = max([int(row["revision"]) for row in prior_rows], default=int(record["payload"].get("plan_revision", 1)))
            revision = max(max_revision, int(record["payload"].get("plan_revision", 1))) + 1
            amendment_id = repo.insert_amendment(conn, record["id"], {
                "revision": revision,
                "amendment_reason": fields["amendment_reason"],
                "changes": {key: value for key, value in fields.items() if key != "amendment_reason"},
                "basis_version": record["version"],
            }, actor.user_id, ws.now)
            ids["amendment_id"] = amendment_id
            ws.apply_record(new_state, dict(record["payload"]), action, {
                "summary": "修订草案已提交，确认前不改变计划依据",
                "input": data, "from": record["state"], "to": new_state,
                "ids": ids, "revision": revision,
            })
            return {"summary": "修订草案已提交", "ids": ids}

        if action == "confirm_amendment":
            amendment_id = integer(data, "amendment_id")
            row = repo.get_amendment_conn(conn, record["id"], amendment_id)
            if row is None:
                raise NotFound("修订不存在")
            if row["status"] != "proposed":
                raise Conflict("修订当前状态为%s，无法确认" % row["status"])
            current_revision = int(record["payload"].get("plan_revision", 1))
            # 只能确认紧接着当前版本的修订；已有更新版本确认后，旧草案即过期。
            if int(row["revision"]) != current_revision + 1:
                raise Conflict(
                    "修订依据版本已过期",
                    {"amendment_id": amendment_id, "revision": int(row["revision"]),
                     "current_revision": current_revision},
                )
            changes = json.loads(row["changes"])
            fields = self.rules.validate_amendment({
                "amendment_reason": row["reason"],
                "updated_goals": changes.get("updated_goals", []),
                **({"service_minutes": changes["service_minutes"]} if "service_minutes" in changes else {}),
            })
            p = dict(record["payload"])
            p["amendment_reason"] = row["reason"]
            p["updated_goals"] = fields["updated_goals"]
            p["goals_count"] = len(fields["updated_goals"])
            if "service_minutes" in fields:
                if int(p["delivered_minutes"]) > fields["service_minutes"]:
                    raise ValidationError("修订后计划分钟数不能低于已交付分钟数")
                p["service_minutes"] = fields["service_minutes"]
            p["plan_revision"] = int(row["revision"])
            p["missing_minutes"] = max(0, int(p["service_minutes"]) - int(p["delivered_minutes"]))
            p["compliance_rate"] = round(int(p["delivered_minutes"]) / int(p["service_minutes"]) * 100, 2)
            p["plan_status"] = "active"
            repo.set_amendment_status(conn, amendment_id, "confirmed", actor.user_id, ws.now)
            ws.apply_record(new_state, p, action, {
                "summary": "修订已确认，计划依据更新到第%d版" % int(row["revision"]),
                "input": {"amendment_id": amendment_id}, "from": record["state"], "to": new_state,
                "ids": {"amendment_id": amendment_id}, "plan_revision": int(row["revision"]),
            })
            self._reconcile_makeup(ws, "计划已更新，旧版未开始补服务失效", batch_key)
            return {"summary": "修订已确认", "ids": {"amendment_id": amendment_id}}

        if action == "confirm_makeup":
            service_id = integer(data, "service_id")
            row = conn.execute(
                "SELECT * FROM service_rows WHERE id=? AND record_id=?", (service_id, record["id"])
            ).fetchone()
            if row is None:
                raise NotFound("补服务记录不存在")
            if row["kind"] != "makeup":
                raise ValidationError("该记录不是补服务")
            if row["status"] != "pending":
                raise Conflict("补服务当前状态为%s" % row["status"])
            confirmed = self.rules.validate_makeup_confirm(data) or int(row["minutes"])
            if confirmed > int(row["minutes"]):
                raise ValidationError("确认分钟数不能超过补服务安排")
            repo.update_service_row_conn(conn, service_id, {
                "status": "confirmed",
                "minutes": confirmed,
                "provider": str(data.get("provider") or actor.user_id),
            }, ws.now)
            total = repo.confirmed_minutes_conn(conn, record["id"])
            p = self.rules.refresh_totals(dict(record["payload"]), total)
            ws.apply_record(new_state, p, action, {
                "summary": "补服务已确认，保留当时计划快照（第%d版）" % int(row["basis_revision"]),
                "input": data, "from": record["state"], "to": new_state,
                "ids": {"service_id": service_id}, "basis_revision": int(row["basis_revision"]),
            })
            self._reconcile_makeup(ws, "补服务已确认，缺口重算", batch_key)
            return {"summary": "补服务已确认", "ids": {"service_id": service_id}}

        raise Conflict("未知动作%s" % action)

    # -- 补服务对账：确定性重算，保证批次重放不产生重复记录 --------------

    def _reconcile_makeup(self, ws, reason: str, batch_key: str) -> None:
        repo = self.repository
        conn = ws.connection
        record = ws.record
        p = record["payload"]
        eligible = record["state"] in MAKEUP_ELIGIBLE_STATES
        gap = int(p.get("missing_minutes", 0))
        revision = int(p.get("plan_revision", 1))
        kept = None
        for row in repo.pending_makeup_rows(conn, record["id"]):
            stale = (not eligible) or gap <= 0 or int(row["basis_revision"]) != revision
            if stale:
                repo.update_service_row_conn(conn, int(row["id"]), {
                    "status": "void", "voided_reason": reason,
                }, ws.now)
            elif kept is None:
                kept = dict(row)
            else:
                repo.update_service_row_conn(conn, int(row["id"]), {
                    "status": "void", "voided_reason": "重复的未开始补服务",
                }, ws.now)
        if eligible and gap > 0 and kept is not None and int(kept["minutes"]) != gap:
            repo.update_service_row_conn(conn, int(kept["id"]), {"minutes": gap}, ws.now)
        if eligible and gap > 0 and kept is None:
            snapshot = self.rules.plan_snapshot(record)
            service_id = repo.insert_service_row(conn, record["id"], {
                "kind": "makeup",
                "minutes": gap,
                "provider": "",
                "status": "pending",
                "basis_revision": revision,
                "basis_snapshot": snapshot,
                "batch_key": batch_key,
            }, ws.now)
            ws.merge_last_audit("makeup_scheduled", {
                "minutes": gap, "plan_revision": revision, "service_id": service_id, "reason": reason,
            })

    # -- 结案缺口：未决异议 / 超期缺口 / 版本不一致 ----------------------

    def _gaps(self, record: Dict[str, Any]) -> List[Dict[str, Any]]:
        with self.repository._connect() as conn:
            open_disputes = [dict(row) for row in conn.execute(
                "SELECT id,topic,status FROM disputes WHERE record_id=? AND status='open' ORDER BY id",
                (record["id"],),
            ).fetchall()]
            revision = int(record["payload"].get("plan_revision", 1))
            stale_rows = [dict(row) for row in conn.execute(
                "SELECT id,basis_revision FROM service_rows"
                " WHERE record_id=? AND status='pending' AND kind='makeup' AND basis_revision<>?",
                (record["id"], revision),
            ).fetchall()]
        return self.rules.open_gaps(record, open_disputes, revision, stale_rows)
