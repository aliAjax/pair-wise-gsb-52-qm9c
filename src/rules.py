"""特殊教育支持计划合规领域规则与状态转换。"""
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .domain import Actor, Conflict, PermissionDenied, ValidationError, boolean, choice, integer, number, text, text_list


INITIAL_STATE = "draft"
CREATE_ROLES = {'case_manager'}
# 争议复核角色的授权范围：review_officer 只能裁定服务与时限类争议，
# 修订类（计划内容）必须由 administrator 裁定。
REVIEW_SCOPES = {
    'administrator': {'service_gap', 'overdue', 'amendment', 'consent'},
    'review_officer': {'service_gap', 'overdue'},
}
DISPUTE_TOPICS = {'service_gap', 'overdue', 'amendment', 'consent'}
ACTION_ROLES = {
    'consent': {'parent_rep'},
    'activate': {'case_manager'},
    'log_service': {'case_manager', 'specialist'},
    'review': {'administrator'},
    'amend': {'case_manager'},
    'close': {'administrator'},
    # 争议处理链动作
    'file_dispute': {'parent_rep'},
    'resolve_dispute': {'administrator', 'review_officer'},
    'withdraw_consent': {'parent_rep'},
    'propose_amendment': {'case_manager'},
    'confirm_amendment': {'case_manager'},
    'confirm_makeup': {'specialist', 'case_manager'},
}
TRANSITIONS = {
    'consent': {'draft': 'consented', 'consent_withdrawn': 'consented'},
    'activate': {'consented': 'active'},
    'log_service': {'active': 'active', 'disputed': 'disputed'},
    'review': {'active': 'under_review'},
    'amend': {'active': 'active', 'under_review': 'active'},
    'close': {'active': 'closed', 'under_review': 'closed'},
    # 家长异议只接受生效中的计划；受理后进入争议状态，冻结未确认修订。
    'file_dispute': {'active': 'disputed'},    # 复核裁定结果：维持回到生效，按修订则回到生效等待新版本，撤回同意单列状态。
    'resolve_dispute': {'disputed': 'active', 'disputed_consent_withdrawn': 'consent_withdrawn'},
    'withdraw_consent': {'active': 'consent_withdrawn', 'disputed': 'disputed_consent_withdrawn'},
    'propose_amendment': {'active': 'active'},
    'confirm_amendment': {'active': 'active', 'under_review': 'active'},
    'confirm_makeup': {'active': 'active', 'under_review': 'under_review', 'disputed': 'disputed'},
}


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def review_scope(self, role: str) -> set:
        if role == "admin":
            return set().union(*REVIEW_SCOPES.values())
        return REVIEW_SCOPES.get(role, set())

    def can_review_topic(self, role: str, topic: str) -> bool:
        return role == "admin" or topic in self.review_scope(role)

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "student_id")
        text(p, "disability")
        integer(p, "service_minutes", 1)
        integer(p, "delivered_minutes", 0)
        integer(p, "review_due_days")
        integer(p, "goals_count", 1)
        boolean(p, "consent")
        if p["delivered_minutes"] > p["service_minutes"]:
            raise ValidationError("已提供服务不能超过计划服务")
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        p["missing_minutes"] = max(0, int(p["service_minutes"]) - int(p["delivered_minutes"]))
        p["compliance_rate"] = round(int(p["delivered_minutes"]) / int(p["service_minutes"]) * 100, 2)
        p["review_overdue"] = int(p["review_due_days"]) <= 0
        p["plan_status"] = "draft"
        # 计划版本（修订号），服务记录按当时版本留快照。
        p["plan_revision"] = 1
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] in {"active", "under_review", "consented", "disputed"} and item["payload"].get("student_id") == payload.get("student_id"):
                raise Conflict("该学生已有有效的支持计划")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def plan_snapshot(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """服务记录/审计引用的计划依据快照。"""
        p = record["payload"]
        return {
            "plan_revision": int(p.get("plan_revision", 1)),
            "plan_version": record.get("version", 1),
            "service_minutes": int(p["service_minutes"]),
            "goals_count": int(p.get("goals_count", 0)),
            "consent": bool(p.get("consent")),
            "consent_scope": p.get("consent_scope", ""),
        }

    # -- 争议链输入校验 --------------------------------------------------

    def validate_dispute(self, data: Dict[str, Any]) -> Dict[str, Any]:
        topic = choice(data, "topic", list(DISPUTE_TOPICS))
        reason = text(data, "reason")
        return {"topic": topic, "reason": reason}

    def validate_resolution(self, data: Dict[str, Any], role: str) -> Dict[str, Any]:
        outcome = choice(data, "outcome", ["uphold_plan", "accept_amendment", "withdraw_consent"])
        note = text(data, "note")
        return {"outcome": outcome, "note": note}

    def validate_withdraw(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return {"reason": text(data, "reason")}

    def validate_amendment(self, data: Dict[str, Any]) -> Dict[str, Any]:
        result = {
            "amendment_reason": text(data, "amendment_reason"),
            "updated_goals": text_list(data, "updated_goals", 1),
        }
        if "service_minutes" in data:
            result["service_minutes"] = integer(data, "service_minutes", 1)
        return result

    def validate_service_log(self, data: Dict[str, Any]) -> int:
        return integer(data, "session_minutes", 1)

    def validate_makeup_confirm(self, data: Dict[str, Any]) -> Optional[int]:
        if "confirmed_minutes" in data and data["confirmed_minutes"] is not None:
            return integer(data, "confirmed_minutes", 1)
        return None

    # -- 既有动作的载荷应用（保持原签名） --------------------------------

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "consent":
            if not boolean(data, "guardian_confirmed"):
                raise ValidationError("监护人尚未确认")
            if not text(data, "consent_scope"):
                raise ValidationError("同意范围不能为空")
            changes["consent"] = True
            changes["consent_scope"] = data["consent_scope"]
            if record["state"] == "consent_withdrawn":
                changes["plan_status"] = "consented"
            summary = "监护人同意已记录"
        elif action == "activate":
            if not p.get("consent"):
                raise ValidationError("缺少有效同意")
            if int(p["goals_count"]) <= 0:
                raise ValidationError("计划必须包含目标")
            changes["plan_status"] = "active"
            summary = "支持计划生效"
        elif action == "log_service":
            session = integer(data, "session_minutes", 1)
            if session + int(p["delivered_minutes"]) > int(p["service_minutes"]):
                raise ValidationError("记录服务超过计划分钟数")
            changes["delivered_minutes"] = int(p["delivered_minutes"]) + session
            changes["last_provider"] = text(data, "provider")
            changes["missing_minutes"] = int(p["service_minutes"]) - changes["delivered_minutes"]
            changes["compliance_rate"] = round(changes["delivered_minutes"] / int(p["service_minutes"]) * 100, 2)
            summary = "服务记录已登记"
        elif action == "review":
            changes["progress_note"] = text(data, "progress_note")
            changes["review_overdue"] = False
            summary = "进入计划复查"
        elif action == "amend":
            changes["amendment_reason"] = text(data, "amendment_reason")
            changes["updated_goals"] = text_list(data, "updated_goals", 1)
            changes["goals_count"] = len(changes["updated_goals"])
            if "service_minutes" in data:
                service_minutes = integer(data, "service_minutes", 1)
                if int(p["delivered_minutes"]) > service_minutes:
                    raise ValidationError("修订后计划分钟数不能低于已交付分钟数")
                changes["service_minutes"] = service_minutes
            changes["missing_minutes"] = max(0, int(changes.get("service_minutes", p["service_minutes"])) - int(p["delivered_minutes"]))
            changes["compliance_rate"] = round(
                int(p["delivered_minutes"]) / int(changes.get("service_minutes", p["service_minutes"])) * 100, 2
            )
            changes["plan_revision"] = int(p.get("plan_revision", 1)) + 1
            changes["plan_status"] = "active"
            summary = "计划已修订"
        elif action == "close":
            if not boolean(data, "review_complete"):
                raise ValidationError("复查尚未完成")
            changes["plan_status"] = "closed"
            summary = "支持计划结束"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)

    # -- 缺口与结案检查 --------------------------------------------------

    def refresh_totals(self, payload: Dict[str, Any], delivered: int) -> Dict[str, Any]:
        service_minutes = int(payload["service_minutes"])
        delivered = min(int(delivered), service_minutes)
        payload = dict(payload)
        payload["delivered_minutes"] = delivered
        payload["missing_minutes"] = max(0, service_minutes - delivered)
        payload["compliance_rate"] = round(delivered / service_minutes * 100, 2) if service_minutes else 0.0
        return payload

    def open_gaps(
        self,
        record: Dict[str, Any],
        open_disputes: List[Dict[str, Any]],
        current_revision: int,
        stale_service_rows: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """结案前必须清零的缺口：未决异议、超期服务缺口、版本不一致。"""
        gaps: List[Dict[str, Any]] = []
        for dispute in open_disputes:
            if dispute["status"] == "open":
                gaps.append({
                    "kind": "open_dispute",
                    "dispute_id": dispute["id"],
                    "topic": dispute["topic"],
                    "detail": "存在未决异议，不能结案",
                })
        p = record["payload"]
        if int(p.get("missing_minutes", 0)) > 0 and bool(p.get("review_overdue")):
            gaps.append({
                "kind": "overdue_service_gap",
                "missing_minutes": int(p["missing_minutes"]),
                "detail": "复查超期且仍有%d分钟服务缺口" % int(p["missing_minutes"]),
            })
        # 版本不一致：服务记录所依据的修订号晚于当前确认修订（修订被撤回/未确认）。
        for row in stale_service_rows:
            gaps.append({
                "kind": "revision_mismatch",
                "service_id": row["id"],
                "basis_revision": int(row.get("basis_revision", 1)),
                "current_revision": int(current_revision),
                "detail": "服务记录依据修订%s与当前版本%s不一致" % (int(row.get("basis_revision", 1)), int(current_revision)),
            })
        return gaps
