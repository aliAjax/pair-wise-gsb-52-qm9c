"""特殊教育支持计划合规领域规则与状态转换。

争议处理链：支持计划 -> 家长异议 -> 受理冻结 -> 计划修订 -> 复核裁定，
服务登记与补服务记录各自保留所依据的计划版本快照。
"""
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .domain import Actor, Conflict, PermissionDenied, ValidationError, boolean, choice, integer, number, optional_text, text, text_list


INITIAL_STATE = "draft"
CREATE_ROLES = {'case_manager'}
ACTION_ROLES = {
    'consent': {'parent_rep'},
    'withdraw_consent': {'parent_rep'},
    'activate': {'case_manager'},
    'log_service': {'case_manager', 'specialist'},
    'review': {'administrator'},
    'amend': {'case_manager'},
    'close': {'administrator'},
}
TRANSITIONS = {
    'consent': {'draft': 'consented', 'active': 'active'},
    'withdraw_consent': {'active': 'active'},
    'activate': {'consented': 'active'},
    'log_service': {'active': 'active'},
    'review': {'active': 'under_review'},
    'amend': {'under_review': 'active'},
    'close': {'active': 'closed', 'under_review': 'closed'},
}

# 争议生命周期
DISPUTE_OPEN = "open"          # 已提交待受理
DISPUTE_ACCEPTED = "accepted"  # 已受理：冻结未确认修订
DISPUTE_UPHELD = "upheld"      # 复核裁定异议成立
DISPUTE_REJECTED = "rejected"  # 复核裁定异议不成立
DISPUTE_STATES = {DISPUTE_OPEN, DISPUTE_ACCEPTED, DISPUTE_UPHELD, DISPUTE_REJECTED}
DISPUTE_PENDING_STATES = {DISPUTE_OPEN, DISPUTE_ACCEPTED}
DISPUTE_DECISIONS = {DISPUTE_UPHELD, DISPUTE_REJECTED}

# 计划修订生命周期
AMEND_PROPOSED = "proposed"    # 修订草案（未确认）
AMEND_FROZEN = "frozen"        # 异议受理后被冻结
AMEND_CONFIRMED = "confirmed"  # 已确认，成为新计划版本
AMEND_DISCARDED = "discarded"  # 异议成立后作废
AMEND_STATES = {AMEND_PROPOSED, AMEND_FROZEN, AMEND_CONFIRMED, AMEND_DISCARDED}

# 补服务记录生命周期
MAKEUP_PLANNED = "planned"     # 未开始
MAKEUP_CONFIRMED = "confirmed"  # 已确认，保留当时快照
MAKEUP_VOID = "void"           # 计划更新/同意撤回后失效
MAKEUP_STATES = {MAKEUP_PLANNED, MAKEUP_CONFIRMED, MAKEUP_VOID}

# 服务台账类型
SERVICE_REGULAR = "regular"
SERVICE_OPENING = "opening"
SERVICE_MAKEUP = "makeup"

# 复核人授权范围：裁定动作与 scope 令牌对应
DECISION_SCOPES = {
    DISPUTE_UPHELD: "dispute:uphold",
    DISPUTE_REJECTED: "dispute:reject",
}
ROLE_SCOPES = {
    "admin": ("dispute:uphold", "dispute:reject", "dispute:any_org"),
    "reviewer": ("dispute:uphold", "dispute:reject"),
}
GLOBAL_ORG_SCOPE = "dispute:any_org"

# 单条补服务建议拆分粒度（分钟），便于逐条确认
MAKEUP_CHUNK_MINUTES = 60


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        all_roles.update(ROLE_SCOPES.keys())
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    # ---- 授权范围 ----
    def scopes_for(self, actor: Actor) -> Tuple[str, ...]:
        # scope 必须显式授予（HTTP 头或测试构造）；admin 角色在具体检查中直接放行
        return tuple(actor.scopes or ())

    def require_review_scope(self, actor: Actor, decision: str, record_org: str) -> None:
        scopes = self.scopes_for(actor)
        required = DECISION_SCOPES.get(decision)
        if required is None or (actor.role != "admin" and required not in scopes):
            raise PermissionDenied("复核人缺少授权范围:%s" % (required or decision))
        if actor.role != "admin" and GLOBAL_ORG_SCOPE not in scopes:
            if not actor.organization or actor.organization != record_org:
                raise PermissionDenied("复核人无权裁定其他机构的争议")

    # ---- 创建 ----
    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "student_id")
        text(p, "disability")
        integer(p, "service_minutes", 1)
        integer(p, "delivered_minutes", 0)
        integer(p, "review_due_days", 0)
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
        p["plan_version"] = 1
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] in {"active", "under_review", "consented"} and item["payload"].get("student_id") == payload.get("student_id"):
                raise Conflict("该学生已有有效的支持计划")

    # ---- 状态机 ----
    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

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
            summary = "监护人同意已记录"
        elif action == "withdraw_consent":
            if not p.get("consent"):
                raise ValidationError("当前没有可撤回的同意")
            if not boolean(data, "guardian_confirmed"):
                raise ValidationError("监护人尚未确认撤回")
            changes["consent"] = False
            reason = optional_text(data, "reason")
            if reason:
                changes["consent_withdrawn_reason"] = reason
            summary = "监护人同意已撤回，未开始的补服务失效"
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
            summary = "服务记录已登记（依据计划版本v%s）" % int(p.get("plan_version", 1))
        elif action == "review":
            changes["progress_note"] = text(data, "progress_note")
            changes["review_overdue"] = False
            summary = "进入计划复查"
        elif action == "amend":
            if not p.get("consent"):
                raise ValidationError("同意已撤回，不能修订")
            changes["amendment_reason"] = text(data, "amendment_reason")
            updated_goals = text_list(data, "updated_goals", 1)
            changes["updated_goals"] = updated_goals
            changes["goals_count"] = len(updated_goals)
            if "service_minutes" in data:
                new_minutes = integer(data, "service_minutes", 1)
                if new_minutes < int(p["delivered_minutes"]):
                    raise ValidationError("修订后计划分钟数不能少于已提供服务")
                changes["service_minutes"] = new_minutes
            changes["plan_status"] = "active"
            changes["plan_version"] = int(p.get("plan_version", 1)) + 1
            summary = "计划已修订（v%s），未开始的补服务重算" % changes["plan_version"]
        elif action == "close":
            if not boolean(data, "review_complete"):
                raise ValidationError("复查尚未完成")
            changes["plan_status"] = "closed"
            summary = "支持计划结束"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)

    # ---- 家长异议 ----
    DISPUTABLE_STATES = {"active", "under_review"}
    AMENDABLE_STATES = {"active", "under_review"}

    def require_disputable(self, record: Dict[str, Any]) -> None:
        if record["state"] not in self.DISPUTABLE_STATES:
            raise Conflict("仅生效或复查中的计划可以提出异议")

    def require_amendable(self, record: Dict[str, Any]) -> None:
        if record["state"] not in self.AMENDABLE_STATES:
            raise Conflict("仅生效或复查中的计划可以修订")

    def validate_dispute(self, data: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "reason": text(data, "reason"),
            "detail": str(data.get("detail", "") or "").strip(),
        }

    def validate_decision_note(self, data: Dict[str, Any]) -> str:
        return text(data, "decision_note")

    # ---- 计划修订 ----
    def validate_amendment(self, data: Dict[str, Any]) -> Dict[str, Any]:
        result = {
            "reason": text(data, "amendment_reason"),
            "updated_goals": text_list(data, "updated_goals", 1),
        }
        if "service_minutes" in data and data.get("service_minutes") is not None:
            result["service_minutes"] = integer(data, "service_minutes", 1)
        return result

    def apply_amendment(self, payload: Dict[str, Any], amendment: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        if not p.get("consent"):
            raise ValidationError("同意已撤回，不能确认修订")
        new_version = int(p.get("plan_version", 1)) + 1
        p["amendment_reason"] = amendment["reason"]
        p["updated_goals"] = list(amendment["updated_goals"])
        p["goals_count"] = len(amendment["updated_goals"])
        if amendment.get("service_minutes") is not None:
            if int(amendment["service_minutes"]) < int(p["delivered_minutes"]):
                raise ValidationError("修订后计划分钟数不能少于已提供服务")
            p["service_minutes"] = int(amendment["service_minutes"])
        p["missing_minutes"] = max(0, int(p["service_minutes"]) - int(p["delivered_minutes"]))
        p["compliance_rate"] = round(int(p["delivered_minutes"]) / int(p["service_minutes"]) * 100, 2)
        p["plan_status"] = "active"
        p["plan_version"] = new_version
        return p

    # ---- 补服务 ----
    def makeup_gap(self, payload: Dict[str, Any]) -> int:
        return max(0, int(payload.get("service_minutes", 0)) - int(payload.get("delivered_minutes", 0)))

    def planned_chunks(self, gap_minutes: int) -> List[int]:
        gap = int(gap_minutes)
        if gap <= 0:
            return []
        full, remainder = divmod(gap, MAKEUP_CHUNK_MINUTES)
        return [MAKEUP_CHUNK_MINUTES] * full + ([remainder] if remainder else [])

    def reconcile_makeup(self, existing: List[Dict[str, Any]], payload: Dict[str, Any], consent_active: bool = True) -> List[Dict[str, Any]]:
        """返回对补服务记录的处置：{id:动作}。计划更新或同意撤回时，未开始的失效重算，已确认保留快照。

        已确认补服务按实际履约抵扣缺口；期初/常规服务分钟数已计入 payload.delivered_minutes，
        不能在此处重复抵扣。同意撤回时仅失效、不重算，待重新同意并更新计划后再生成。
        """
        gap = self.makeup_gap(payload) if consent_active else 0
        confirmed_makeup = [item for item in existing
                            if item["status"] == MAKEUP_CONFIRMED and item.get("entry_type") == SERVICE_MAKEUP]
        confirmed_minutes = sum(int(item["minutes"]) for item in confirmed_makeup)
        actions: List[Dict[str, Any]] = []
        for item in existing:
            if item["status"] == MAKEUP_PLANNED:
                actions.append({"id": item["id"], "action": "void"})
        remaining_gap = max(0, gap - confirmed_minutes)
        for chunk in self.planned_chunks(remaining_gap):
            actions.append({"action": "create", "minutes": chunk})
        return actions

    # ---- 计划快照 ----
    def snapshot(self, record: Dict[str, Any], label: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        data = {
            "plan_version": int(record["payload"].get("plan_version", 1)),
            "service_minutes": int(record["payload"].get("service_minutes", 0)),
            "delivered_minutes": int(record["payload"].get("delivered_minutes", 0)),
            "goals_count": int(record["payload"].get("goals_count", 0)),
            "consent": bool(record["payload"].get("consent")),
            "label": label,
        }
        if extra:
            data.update(extra)
        return data

    # ---- 结案拦截：未决异议、超期缺口、版本不一致 ----
    def close_blockers(self, record: Dict[str, Any], disputes: List[Dict[str, Any]], entries: List[Dict[str, Any]], pending_amendments: int = 0) -> List[Dict[str, str]]:
        blockers: List[Dict[str, str]] = []
        for dispute in disputes:
            if dispute["status"] in DISPUTE_PENDING_STATES:
                blockers.append({
                    "type": "open_dispute",
                    "code": "dispute_%s" % dispute["status"],
                    "detail": "争议#%s未决（%s）" % (dispute["id"], dispute["status"]),
                })
        p = record["payload"]
        if bool(p.get("review_overdue")):
            blockers.append({
                "type": "overdue_gap",
                "code": "review_overdue",
                "detail": "复查超期，剩余服务缺口%s分钟" % self.makeup_gap(p),
            })
        regular = [e for e in entries if e.get("status") == "confirmed"]
        ledger_minutes = sum(int(e["minutes"]) for e in regular)
        if ledger_minutes != int(p.get("delivered_minutes", 0)):
            blockers.append({
                "type": "version_mismatch",
                "code": "ledger_minutes_mismatch",
                "detail": "服务台账合计%s分钟与计划记录的已提供%s分钟不一致" % (ledger_minutes, int(p.get("delivered_minutes", 0))),
            })
        snapshot_versions = sorted({int(e["basis_version"]) for e in entries})
        current_version = int(p.get("plan_version", 1))
        for version in snapshot_versions:
            if version > current_version:
                blockers.append({
                    "type": "version_mismatch",
                    "code": "snapshot_ahead_of_plan",
                    "detail": "服务记录依据版本v%s高于当前计划版本v%s" % (version, current_version),
                })
        if int(p.get("delivered_minutes", 0)) > int(p.get("service_minutes", 0)):
            blockers.append({
                "type": "version_mismatch",
                "code": "delivered_exceeds_plan",
                "detail": "已提供服务超过计划分钟数",
            })
        if pending_amendments:
            blockers.append({
                "type": "version_mismatch",
                "code": "unconfirmed_amendment",
                "detail": "存在%s份未确认修订，计划版本未对齐" % pending_amendments,
            })
        return blockers
