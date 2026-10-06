"""业务用例编排：争议处理链、权限/授权范围、乐观并发、批次恢复与审计。

链条：支持计划 → 家长异议（受理即冻结未确认修订）→ 计划修订 → 复核裁定；
服务登记与补服务均保存当时计划版本快照。所有跨表写入封装为完整批次，
写入失败后凭批次重试，补服务记录不会重复生成。
"""
import sqlite3
import uuid
from typing import Any, Callable, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, ValidationError, text
from .repository import Repository, TxGateway
from .rules import (
    AMEND_CONFIRMED,
    AMEND_FROZEN,
    AMEND_PROPOSED,
    AMEND_DISCARDED,
    DISPUTE_ACCEPTED,
    DISPUTE_OPEN,
    DISPUTE_PENDING_STATES,
    DISPUTE_REJECTED,
    DISPUTE_UPHELD,
    MAKEUP_CONFIRMED,
    SERVICE_MAKEUP,
    SERVICE_REGULAR,
    DomainRules,
)


class _WorkerCtx:
    def __init__(self, batch_id: str, attempt: int) -> None:
        self.batch_id = batch_id
        self.attempt = attempt  # 1 表示首次执行，>1 表示崩溃后的恢复重放


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    # ---- 身份 ----
    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    # ---- 完整批次执行 ----
    def _run_batch(self, record_id: int, operation: str, request_payload: Dict[str, Any], actor: Actor,
                   worker: Callable[[TxGateway, _WorkerCtx], Dict[str, Any]], batch_id: Optional[str]) -> Dict[str, Any]:
        batch_id = (batch_id or "").strip() or ("auto-%s" % uuid.uuid4().hex)
        try:
            with self.repository.transaction() as tx:
                existing = tx.reserve_batch(batch_id, record_id, operation, request_payload, actor.user_id)
                if existing is not None:
                    if existing["operation"] != operation or int(existing["record_id"]) != int(record_id):
                        raise Conflict("批次%s已用于其他操作" % batch_id)
                    if existing["status"] == "committed":
                        # 提交成功但响应丢失：直接回放首次结果，不重复任何写入
                        return TxGateway.batch_dict(existing)["response_payload"]
                    # status=reserved：上次在提交前崩溃，本事务内完整重放
                    attempt = int(existing["attempts"]) + 1
                    tx.mark_batch(batch_id, "reserved", increment_attempt=True)
                else:
                    attempt = 1
                response = worker(tx, _WorkerCtx(batch_id, attempt))
                tx.mark_batch(batch_id, "committed", response)
                self.repository.commit(tx)
            return response
        except sqlite3.OperationalError as exc:
            # 注入的提交点失败或数据库级错误：客户端凭同一批次重试即可恢复
            raise Conflict("写入中断，请凭批次%s重试以恢复" % batch_id, {"batch_id": batch_id, "recoverable": True, "message": str(exc)})

    # ---- 记录创建/读取 ----
    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id, actor.organization)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    # ---- 基础动作（consent/activate/log_service/review/amend/close/withdraw_consent）----
    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any],
            batch_id: Optional[str] = None) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        data = dict(data or {})

        def worker(tx: TxGateway, ctx: _WorkerCtx) -> Dict[str, Any]:
            record = tx.get_record(record_id)
            # 结案先算缺口：未决异议/超期/版本不一致必须在版本冲突之前暴露详情
            if action == "close":
                blockers = self._blockers_in_tx(tx, record)
                if blockers:
                    raise Conflict("存在结案缺口，无法结案", {"blockers": blockers})
            self._assert_version(record, expected_version, data)
            new_state, new_payload, summary = self.rules.apply_action(record, action, data)

            if action == "log_service":
                session = int(data["session_minutes"])
                entry_id = tx.insert_service_entry(
                    record_id, SERVICE_REGULAR, session, int(new_payload.get("plan_version", 1)),
                    text(data, "provider"), "log:%s:%d" % (ctx.batch_id, session),
                    self.rules.snapshot({"payload": new_payload, "state": new_state}, "service-logged"),
                )
                self._reconcile_makeup(tx, record_id, new_payload, ctx.batch_id)
                extra = {"service_entry_id": entry_id}
            elif action == "amend":
                self._reconcile_makeup(tx, record_id, new_payload, ctx.batch_id)
                tx.insert_snapshot(record_id, int(new_payload["plan_version"]), "amend-confirmed",
                                   self.rules.snapshot({"payload": new_payload}, "amend-confirmed"), actor.user_id)
                extra = {"plan_version": new_payload["plan_version"]}
            elif action == "withdraw_consent":
                voided = tx.void_planned_makeup(record_id)
                extra = {"voided_makeup": voided, "regenerated": 0}
            else:
                extra = {}

            version = tx.bump_record(record_id, expected_version, new_state, new_payload, actor.user_id)
            tx.add_log(record_id, actor.user_id, action, version,
                       {"summary": summary, "input": data, "from": record["state"], "to": new_state, **extra},
                       idem_key="audit:%s" % ctx.batch_id)
            result = tx.get_record(record_id)
            result["audit_version"] = version
            result["summary"] = summary
            result.update(extra)
            return result

        return self._run_batch(record_id, "action:%s" % action, {"expected_version": expected_version, "data": data},
                               actor, worker, batch_id)

    # ---- 家长异议 ----
    def file_dispute(self, actor: Actor, record_id: int, expected_version: int, data: Dict[str, Any],
                     batch_id: Optional[str] = None) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if actor.role != "admin" and actor.role != "parent_rep":
            raise PermissionDenied("仅监护人代表可提交异议")
        content = self.rules.validate_dispute(data or {})

        def worker(tx: TxGateway, ctx: _WorkerCtx) -> Dict[str, Any]:
            record = tx.get_record(record_id)
            self.rules.require_disputable(record)
            self._assert_version(record, expected_version, {"reason": content["reason"], "detail": content["detail"]})
            dispute_id = tx.insert_dispute(record_id, content["reason"], content["detail"],
                                           actor.user_id, record["version"])
            version = tx.bump_record(record_id, expected_version, None, record["payload"], actor.user_id)
            tx.add_log(record_id, actor.user_id, "dispute_filed", version,
                       {"dispute_id": dispute_id, "reason": content["reason"], "detail": content["detail"],
                        "plan_version": record["payload"].get("plan_version", 1)},
                       idem_key="audit:%s" % ctx.batch_id)
            return {"dispute_id": dispute_id, "status": DISPUTE_OPEN, "record_version": version, **content}

        return self._run_batch(record_id, "dispute:file", {"expected_version": expected_version, "data": data},
                               actor, worker, batch_id)

    def accept_dispute(self, actor: Actor, dispute_id: int, batch_id: Optional[str] = None) -> Dict[str, Any]:
        """受理异议：冻结全部未确认修订；已有服务与审计保留原依据不变。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if actor.role not in ("admin", "administrator"):
            raise PermissionDenied("仅管理员可受理异议")

        def worker(tx: TxGateway, ctx: _WorkerCtx) -> Dict[str, Any]:
            dispute = tx.lock_dispute(dispute_id)
            record_id = int(dispute["record_id"])
            if dispute["status"] != DISPUTE_OPEN:
                raise Conflict("争议已受理或已裁定", {"dispute_status": dispute["status"]})
            frozen = []
            for amendment in tx.list_amendments(record_id, [AMEND_PROPOSED]):
                tx.update_amendment(amendment["id"], AMEND_FROZEN)
                frozen.append(amendment["id"])
            tx.update_dispute(dispute_id, DISPUTE_ACCEPTED, actor.user_id)
            record = tx.get_record(record_id)
            version = tx.bump_record(record_id, record["version"], None, record["payload"], actor.user_id)
            tx.add_log(record_id, actor.user_id, "dispute_accepted", version,
                       {"dispute_id": dispute_id, "frozen_amendments": frozen,
                        "summary": "异议已受理，未确认修订冻结；已有服务与审计保留原依据"},
                       idem_key="audit:%s" % ctx.batch_id)
            return {"dispute_id": dispute_id, "status": DISPUTE_ACCEPTED, "frozen_amendments": frozen,
                    "record_version": version}

        # 批次仍需挂在具体记录上：先读取争议定位记录（只读）
        record_id = self._dispute_record_id(dispute_id)
        return self._run_batch(record_id, "dispute:accept", {"dispute_id": dispute_id}, actor, worker, batch_id)

    def decide_dispute(self, actor: Actor, dispute_id: int, data: Dict[str, Any],
                       batch_id: Optional[str] = None) -> Dict[str, Any]:
        """复核人按授权范围裁定：uphold 冻结修订作废；reject 解冻回到待确认。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        data = dict(data or {})
        decision = DISPUTE_UPHELD if data.get("decision") == "upheld" else DISPUTE_REJECTED if data.get("decision") == "rejected" else None
        if decision is None:
            raise ValidationError("decision只能是upheld/rejected")
        note = self.rules.validate_decision_note(data)
        record_id = self._dispute_record_id(dispute_id)
        record = self.repository.get(record_id)
        self.rules.require_review_scope(actor, decision, record.get("owner_org", ""))

        def worker(tx: TxGateway, ctx: _WorkerCtx) -> Dict[str, Any]:
            dispute = tx.lock_dispute(dispute_id)
            rid = int(dispute["record_id"])
            if dispute["status"] != DISPUTE_ACCEPTED:
                raise Conflict("仅已受理的争议可以裁定", {"dispute_status": dispute["status"]})
            target_status = AMEND_DISCARDED if decision == DISPUTE_UPHELD else AMEND_PROPOSED
            touched = []
            for amendment in tx.list_amendments(rid, [AMEND_FROZEN]):
                tx.update_amendment(amendment["id"], target_status)
                touched.append(amendment["id"])
            tx.update_dispute(dispute_id, decision, actor.user_id, note)
            current = tx.get_record(rid)
            version = tx.bump_record(rid, current["version"], None, current["payload"], actor.user_id)
            tx.add_log(rid, actor.user_id, "dispute_%s" % decision, version,
                       {"dispute_id": dispute_id, "decision": decision, "decision_note": note,
                        "amendments": touched,
                        "summary": "异议裁定%s，%s" % ("成立" if decision == DISPUTE_UPHELD else "不成立",
                                                "冻结修订作废" if decision == DISPUTE_UPHELD else "修订解除冻结")},
                       idem_key="audit:%s" % ctx.batch_id)
            return {"dispute_id": dispute_id, "status": decision, "amendments": touched,
                    "decision_note": note, "record_version": version}

        return self._run_batch(record_id, "dispute:decide", {"dispute_id": dispute_id, "data": data},
                               actor, worker, batch_id)

    def list_disputes(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.disputes(record_id)

    # ---- 计划修订（未确认修订可被争议冻结）----
    def propose_amendment(self, actor: Actor, record_id: int, expected_version: int, data: Dict[str, Any],
                          batch_id: Optional[str] = None) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_action(actor.role, "amend"):
            raise PermissionDenied("角色无权提出修订")
        content = self.rules.validate_amendment(data or {})
        draft = {"amendment_reason": content["reason"], "updated_goals": content["updated_goals"],
                 "service_minutes": content.get("service_minutes")}

        def worker(tx: TxGateway, ctx: _WorkerCtx) -> Dict[str, Any]:
            record = tx.get_record(record_id)
            self.rules.require_amendable(record)
            self._assert_version(record, expected_version, draft)
            self._assert_no_pending_dispute(tx, record_id)
            base_version = int(record["payload"].get("plan_version", 1))
            amendment_id = tx.insert_amendment(record_id, expected_version, base_version, content, actor.user_id)
            version = tx.bump_record(record_id, expected_version, None, record["payload"], actor.user_id)
            tx.add_log(record_id, actor.user_id, "amendment_proposed", version,
                       {"amendment_id": amendment_id, "base_version": base_version, "content": content,
                        "summary": "计划修订草案已提交（v%s基线，待确认）" % base_version},
                       idem_key="audit:%s" % ctx.batch_id)
            return {"amendment_id": amendment_id, "status": AMEND_PROPOSED, "base_version": base_version,
                    "record_version": version, "content": content}

        return self._run_batch(record_id, "amendment:propose", {"expected_version": expected_version, "data": data},
                               actor, worker, batch_id)

    def confirm_amendment(self, actor: Actor, amendment_id: int, expected_version: Optional[int] = None,
                          batch_id: Optional[str] = None) -> Dict[str, Any]:
        """确认修订：计划升版并留快照；未开始补服务失效重算，已确认补服务保留当时快照。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_action(actor.role, "amend"):
            raise PermissionDenied("角色无权确认修订")

        def worker(tx: TxGateway, ctx: _WorkerCtx) -> Dict[str, Any]:
            amendment = tx.lock_amendment(amendment_id)
            record_id = int(amendment["record_id"])
            record = tx.get_record(record_id)
            if expected_version is not None and int(expected_version) != int(record["version"]):
                raise Conflict("版本冲突，请刷新后重试", {"current_version": record["version"]})
            content = tx.amendment_dict(amendment)["content"]
            status = amendment["status"]
            if status == AMEND_FROZEN:
                raise Conflict("修订已被异议冻结，需等复核裁定", {"amendment_status": status})
            if status == AMEND_DISCARDED:
                raise Conflict("修订已随异议成立而作废", {"amendment_status": status})
            if status == AMEND_CONFIRMED:
                raise Conflict("修订已确认", {"amendment_status": status, "confirmed_version": amendment["confirmed_version"]})
            self._assert_no_pending_dispute(tx, record_id)
            if int(amendment["base_version"]) != int(record["payload"].get("plan_version", 1)):
                raise Conflict("修订基线版本与当前计划不一致，请重新基于v%s填写"
                               % record["payload"].get("plan_version", 1),
                               {"current_plan_version": record["payload"].get("plan_version", 1),
                                "base_version": amendment["base_version"], "draft": content})
            new_payload = self.rules.apply_amendment(record["payload"], content)
            new_state = "active"
            version = tx.bump_record(record_id, record["version"], new_state, new_payload, actor.user_id)
            tx.update_amendment(amendment_id, AMEND_CONFIRMED, int(new_payload["plan_version"]))
            tx.insert_snapshot(record_id, int(new_payload["plan_version"]), "amendment-confirmed",
                               self.rules.snapshot({"payload": new_payload}, "amendment-confirmed",
                                                   {"amendment_id": amendment_id}), actor.user_id)
            plan_actions = self._reconcile_makeup(tx, record_id, new_payload, ctx.batch_id)
            tx.add_log(record_id, actor.user_id, "amendment_confirmed", version,
                       {"amendment_id": amendment_id, "plan_version": new_payload["plan_version"],
                        "makeup": plan_actions, "summary": "修订已确认，计划升版；未开始补服务失效重算，已确认补服务保留快照"},
                       idem_key="audit:%s" % ctx.batch_id)
            result = tx.get_record(record_id)
            result["amendment_id"] = amendment_id
            result["makeup"] = plan_actions
            return result

        record_id = self._amendment_record_id(amendment_id)
        return self._run_batch(record_id, "amendment:confirm", {"amendment_id": amendment_id}, actor, worker, batch_id)

    def list_amendments(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.amendments(record_id)

    # ---- 服务台账与补服务 ----
    def service_entries(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.service_entries(record_id)

    def snapshots(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.snapshots(record_id)

    def confirm_makeup(self, actor: Actor, makeup_id: int, data: Dict[str, Any],
                       batch_id: Optional[str] = None) -> Dict[str, Any]:
        """确认补服务已完成：保留确认当时的计划版本快照，并计入履约台账。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_action(actor.role, "log_service"):
            raise PermissionDenied("角色无权确认补服务")
        provider = text(data or {}, "provider")

        def worker(tx: TxGateway, ctx: _WorkerCtx) -> Dict[str, Any]:
            row = tx.lock_makeup(makeup_id)
            record_id = int(row["record_id"])
            record = tx.get_record(record_id)
            if row["status"] == MAKEUP_CONFIRMED:
                return {"makeup_id": makeup_id, "status": MAKEUP_CONFIRMED, "deduplicated": True,
                        "basis_version": row["basis_version"]}
            if row["status"] == "void":
                raise Conflict("补服务记录已失效，请按最新计划重算", {"makeup_status": "void"})
            if not record["payload"].get("consent"):
                raise ValidationError("同意已撤回，不能确认补服务")
            minutes = int(row["minutes"])
            payload = dict(record["payload"])
            if int(payload["delivered_minutes"]) + minutes > int(payload["service_minutes"]):
                raise ValidationError("确认补服务后将超过计划分钟数")
            payload["delivered_minutes"] = int(payload["delivered_minutes"]) + minutes
            payload["missing_minutes"] = int(payload["service_minutes"]) - payload["delivered_minutes"]
            payload["compliance_rate"] = round(payload["delivered_minutes"] / int(payload["service_minutes"]) * 100, 2)
            snapshot = self.rules.snapshot({"payload": payload}, "makeup-confirmed", {"makeup_id": makeup_id})
            tx.confirm_makeup(makeup_id, int(payload.get("plan_version", 1)), snapshot, provider)
            version = tx.bump_record(record_id, record["version"], record["state"], payload, actor.user_id)
            tx.add_log(record_id, actor.user_id, "makeup_confirmed", version,
                       {"makeup_id": makeup_id, "minutes": minutes, "provider": provider,
                        "basis_version": payload.get("plan_version", 1),
                        "summary": "补服务已确认，保留v%s快照" % payload.get("plan_version", 1)},
                       idem_key="audit:%s" % ctx.batch_id)
            return {"makeup_id": makeup_id, "status": MAKEUP_CONFIRMED, "minutes": minutes,
                    "basis_version": payload.get("plan_version", 1), "record_version": version}

        record_id = self._makeup_record_id(makeup_id)
        return self._run_batch(record_id, "makeup:confirm", {"makeup_id": makeup_id, "data": data or {}},
                               actor, worker, batch_id)

    # ---- 审计/统计 ----
    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()

    # ---- 结案就绪检查：未决异议 / 超期缺口 / 版本不一致 ----
    def readiness(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        record = self.repository.get(record_id)
        amendments = self.repository.amendments(record_id)
        pending_count = sum(1 for item in amendments if item["status"] in (AMEND_PROPOSED, AMEND_FROZEN))
        blockers = self.rules.close_blockers(
            record,
            self.repository.disputes(record_id),
            self.repository.service_entries(record_id),
            pending_count,
        )
        return {"record_id": record_id, "can_close": not blockers, "blockers": blockers,
                "gap_minutes": self.rules.makeup_gap(record["payload"]),
                "plan_version": record["payload"].get("plan_version", 1)}

    # ---- 批次恢复查询 ----
    def get_batch(self, actor: Actor, batch_id: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        with self.repository._connect() as connection:
            row = connection.execute("SELECT * FROM write_batches WHERE batch_id=?", (batch_id,)).fetchone()
        if row is None:
            from .domain import NotFound
            raise NotFound("批次不存在")
        return TxGateway.batch_dict(row)

    # ---- 内部辅助 ----
    def _dispute_record_id(self, dispute_id: int) -> int:
        with self.repository._connect() as connection:
            row = connection.execute("SELECT record_id FROM disputes WHERE id=?", (dispute_id,)).fetchone()
        if row is None:
            from .domain import NotFound
            raise NotFound("争议不存在")
        return int(row["record_id"])

    def _amendment_record_id(self, amendment_id: int) -> int:
        with self.repository._connect() as connection:
            row = connection.execute("SELECT record_id FROM amendments WHERE id=?", (amendment_id,)).fetchone()
        if row is None:
            from .domain import NotFound
            raise NotFound("修订不存在")
        return int(row["record_id"])

    def _makeup_record_id(self, makeup_id: int) -> int:
        with self.repository._connect() as connection:
            row = connection.execute("SELECT record_id FROM service_entries WHERE id=? AND entry_type='makeup'",
                                     (makeup_id,)).fetchone()
        if row is None:
            from .domain import NotFound
            raise NotFound("补服务记录不存在")
        return int(row["record_id"])

    @staticmethod
    def _assert_version(record: Dict[str, Any], expected_version: int, draft: Any) -> None:
        if int(expected_version) != int(record["version"]):
            raise Conflict("版本冲突，请刷新后重试；您填写的内容已保留",
                           {"current_version": record["version"], "expected_version": int(expected_version),
                            "draft": draft})

    def _assert_no_pending_dispute(self, tx: TxGateway, record_id: int) -> None:
        for dispute in tx.list_disputes(record_id):
            if dispute["status"] in DISPUTE_PENDING_STATES:
                raise Conflict("存在未决争议#%s（%s），修订冻结中" % (dispute["id"], dispute["status"]),
                               {"dispute_id": dispute["id"], "dispute_status": dispute["status"]})

    def _reconcile_makeup(self, tx: TxGateway, record_id: int, payload: Dict[str, Any], batch_id: str) -> List[Dict[str, Any]]:
        existing = tx.list_service_entries(record_id)
        actions = self.rules.reconcile_makeup(existing, payload, consent_active=bool(payload.get("consent")))
        result: List[Dict[str, Any]] = []
        for action in actions:
            if action["action"] == "void":
                result.append({"id": action["id"], "action": "void"})
        voided = tx.void_planned_makeup(record_id)
        index = 0
        for action in actions:
            if action["action"] != "create":
                continue
            idem_key = "makeup:%s:%d" % (batch_id, index)
            existing_entry = tx.get_entry_by_idem(record_id, idem_key)
            if existing_entry is not None:
                result.append({"id": existing_entry["id"], "action": "restored", "minutes": existing_entry["minutes"]})
            else:
                entry_id = tx.insert_service_entry(
                    record_id, SERVICE_MAKEUP, int(action["minutes"]), int(payload.get("plan_version", 1)),
                    "", idem_key, self.rules.snapshot({"payload": payload}, "makeup-planned", {"idem_key": idem_key}),
                )
                result.append({"id": entry_id, "action": "created", "minutes": int(action["minutes"])})
            index += 1
        return result

    def _blockers_in_tx(self, tx: TxGateway, record: Dict[str, Any]) -> List[Dict[str, str]]:
        entries = tx.list_service_entries(record["id"])
        disputes = tx.list_disputes(record["id"])
        amendments = tx.list_amendments(record["id"])
        pending = sum(1 for item in amendments if item["status"] in (AMEND_PROPOSED, AMEND_FROZEN))
        return self.rules.close_blockers(record, disputes, entries, pending)
