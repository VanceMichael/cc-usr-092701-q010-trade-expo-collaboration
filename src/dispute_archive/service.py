"""争议归档与分流核心服务。

一个服务实例持有一条全局哈希链日志与按 ``order_id`` 派生的案件投影。
所有判定都可通过重放日志复现；跨重启恢复使用 :meth:`DisputeService.recover`。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from .errors import (
    AuthorizationError,
    ImmutabilityError,
    IntakeValidationError,
    RoutingBlocked,
)
from .events import (
    AUTO_FROZEN,
    CASE_OPENED,
    CASE_STATUS,
    DATA_ACCESS,
    DATA_CONSENT,
    DATA_TRANSFER,
    DEADLINE_UPDATE,
    DECISION_MADE,
    FEE_UPDATE,
    GRANT_ADDED,
    NOTICE_SENT,
    OPERATOR_FACT,
    ORDER_EVENT,
    PARTY_STATEMENT,
    ROUTING_DECIDED,
    SIGNATURE_EVIDENCE,
    SUPPLEMENTAL_AGREEMENT,
    AGENT_AUTHORIZATION,
    BUSINESS_TYPES,
    validate_event,
)
from .journal import Entry, Journal
from .rules import RuleRegistry
from .state import (
    STATUS_LITIGATION,
    STATUS_MEDIATION,
    STATUS_WITHDRAWN,
    Case,
    Decision,
    apply_business,
    apply_management,
    evaluate_routing,
)

# 角色：
#   party        当事人（买方/卖方），可提交陈述、授权原件访问
#   mediator     调解员，只能查看获授权的原件
#   operator     平台运营者，可补充交易事实但无权改写陈述、无权看原件
#   coordinator  协调员，推进期限/通知/费用、发起跨境协作
#   arbitrator   裁定人，作出裁定、切换案件状态
ROLE_PARTY = "party"
ROLE_MEDIATOR = "mediator"
ROLE_OPERATOR = "operator"
ROLE_COORDINATOR = "coordinator"
ROLE_ARBITRATOR = "arbitrator"

_VALID_ROLES = frozenset(
    {ROLE_PARTY, ROLE_MEDIATOR, ROLE_OPERATOR, ROLE_COORDINATOR, ROLE_ARBITRATOR}
)

# 证据原件类型 -> 授权标签
_DOC_GRANTS: dict[str, str] = {
    SIGNATURE_EVIDENCE: "signature",
    "payment_record": "payment",
    DATA_CONSENT: "data_consent",
    AGENT_AUTHORIZATION: "agent_auth",
}
_GRANT_LABELS = frozenset(_DOC_GRANTS.values())

TERMINAL_STATUSES = frozenset({STATUS_MEDIATION, STATUS_LITIGATION, STATUS_WITHDRAWN})
ROUTE_PENDING = "pending"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _case_id(order_id: str) -> str:
    return "DS-" + hashlib.sha256(order_id.encode("utf-8")).hexdigest()[:12].upper()


def _decision_id(order_id: str, matter: str) -> str:
    return "DEC-" + hashlib.sha256((order_id + "|" + matter).encode("utf-8")).hexdigest()[:10].upper()


@dataclass
class SubmissionResult:
    case_id: str
    order_id: str
    duplicate: bool
    created: bool
    route: str | None
    frozen: bool
    freeze_reasons: list[str]
    rule_version: str | None
    seq: int


class DisputeService:
    def __init__(self, registry: RuleRegistry, *, clock: Callable[[], str] = _now):
        self._journal = Journal()
        self._registry = registry
        self._clock = clock
        self._cases: dict[str, Case] = {}
        self._order_by_event: dict[str, str] = {}

    # ---- 恢复 -------------------------------------------------------------
    @classmethod
    def recover(cls, journal_text: str, registry: RuleRegistry, *, verify: bool = True) -> "DisputeService":
        journal = Journal.from_jsonl(journal_text, verify=verify)
        svc = cls(registry)
        svc._journal = journal
        for entry in journal:
            oid = entry.payload.get("order_id")
            if not oid:
                continue
            if entry.event_type == CASE_OPENED and oid not in svc._cases:
                svc._cases[oid] = Case(
                    case_id=entry.payload["case_id"],
                    order_id=oid,
                    rule_version=entry.payload["rule_version"],
                )
            if entry.event_type in BUSINESS_TYPES:
                svc._order_by_event[entry.event_id] = oid
        for oid, case in svc._cases.items():
            for entry in journal:
                if entry.payload.get("order_id") != oid:
                    continue
                if entry.event_type in BUSINESS_TYPES:
                    apply_business(case, entry)
                else:
                    apply_management(case, entry)
        return svc

    @property
    def journal(self) -> Journal:
        return self._journal

    @property
    def registry(self) -> RuleRegistry:
        return self._registry

    def export_log(self) -> str:
        return self._journal.export_jsonl()

    # ---- 入站：幂等归档 + 自动分流 ----------------------------------------
    def submit(self, event: dict, *, actor: str = "external") -> SubmissionResult:
        inc = validate_event(event)
        oid = inc.payload["order_id"]

        # 相同事件重复提交：按 event_id 幂等，直接返回原案件，不再写日志。
        if inc.event_id in self._order_by_event:
            existing = self._cases[oid]
            return SubmissionResult(
                case_id=existing.case_id,
                order_id=oid,
                duplicate=True,
                created=False,
                route=existing.route,
                frozen=existing.frozen,
                freeze_reasons=list(existing.freeze_reasons),
                rule_version=existing.rule_version,
                seq=len(self._journal),
            )

        created = False
        case = self._cases.get(oid)
        if case is None:
            if inc.event_type != ORDER_EVENT:
                raise IntakeValidationError(f"收到 {inc.event_type}，但订单事件尚未归档：{oid}")
            case = self._open_case(inc)
            created = True

        entry = self._append(inc.event_type, inc.payload, inc.event_id, inc.timestamp, actor)
        apply_business(case, entry)
        self._order_by_event[inc.event_id] = oid
        self._run_routing(case)

        return SubmissionResult(
            case_id=case.case_id,
            order_id=oid,
            duplicate=False,
            created=created,
            route=case.route,
            frozen=case.frozen,
            freeze_reasons=list(case.freeze_reasons),
            rule_version=case.rule_version,
            seq=entry.seq,
        )

    def _open_case(self, inc) -> Case:
        oid = inc.payload["order_id"]
        occurred_at = inc.payload["occurred_at"]
        # 适用案件发生时有效的规则版本，之后不再漂移。
        rv = self._registry.version_in_force_at(occurred_at)
        case = Case(case_id=_case_id(oid), order_id=oid, rule_version=rv.version)
        self._cases[oid] = case
        self._append(
            CASE_OPENED,
            {
                "case_id": case.case_id,
                "order_id": oid,
                "rule_version": rv.version,
                "occurred_at": occurred_at,
            },
            f"open-{oid}",
            inc.timestamp,
            "system",
        )
        return case

    def _run_routing(self, case: Case) -> None:
        # 新证据进入后重新计算；冻结有黏性，新矛盾理由可叠加进同一条冻结轨迹。
        for effect in evaluate_routing(case):
            if effect["kind"] == "frozen":
                entry = self._append(
                    AUTO_FROZEN,
                    {"kind": "frozen", "reasons": effect["reasons"], "order_id": case.order_id},
                    f"freeze-{case.order_id}-{len(self._journal) + 1}",
                    self._clock(),
                    "system",
                )
                apply_management(case, entry)
            else:
                entry = self._append(
                    ROUTING_DECIDED,
                    {"kind": "routed", "route": effect["route"], "order_id": case.order_id},
                    f"route-{case.order_id}-{effect['route']}-{len(self._journal) + 1}",
                    self._clock(),
                    "system",
                )
                apply_management(case, entry)

    def _append(self, event_type: str, payload: dict, event_id: str, timestamp: str, actor: str) -> Entry:
        return self._journal.append(
            timestamp=timestamp, event_type=event_type, payload=payload, event_id=event_id, actor=actor
        )

    # ---- 查询 -------------------------------------------------------------
    def get_case(self, order_id: str) -> Case:
        if order_id not in self._cases:
            raise KeyError(order_id)
        return self._cases[order_id]

    def require_routing(self, order_id: str) -> str:
        """返回自动分流目标；材料矛盾冻结时抛错（冻结自动分流）。"""
        case = self.get_case(order_id)
        if case.frozen:
            raise RoutingBlocked(
                "签名或支付材料互相矛盾，自动分流已冻结，转人工核档",
                code="frozen",
                reasons=list(case.freeze_reasons),
            )
        return case.route if case.route is not None else ROUTE_PENDING

    # ---- 补充协议：只能改变尚未裁定的事项，不能覆盖已使用的证据 -----------
    def add_supplemental(self, event: dict, *, actor: str = "external") -> Entry:
        inc = validate_event(event)
        if inc.event_type != SUPPLEMENTAL_AGREEMENT:
            raise IntakeValidationError("此接口只接收补充协议")
        case = self.get_case(inc.payload["order_id"])
        changes = inc.payload["changes"]
        decided = sorted({c["matter"] for c in changes if c["matter"] in case.decided_matters})
        if decided:
            raise ImmutabilityError("补充协议不能改变已裁定事项：" + ",".join(decided))
        touched = sorted(
            {
                c["matter"]
                for c in changes
                if any(ref in case.locked_evidence for ref in c.get("evidence_refs", []))
            }
        )
        if touched:
            raise ImmutabilityError("补充协议不能覆盖已被裁定使用的证据：" + ",".join(touched))
        entry = self._append(SUPPLEMENTAL_AGREEMENT, inc.payload, inc.event_id, inc.timestamp, actor)
        apply_business(case, entry)
        self._order_by_event[inc.event_id] = case.order_id
        return entry

    # ---- 裁定 -------------------------------------------------------------
    def make_decision(
        self,
        *,
        order_id: str,
        matter: str,
        ruling: str,
        rule_ids: list[str],
        evidence_refs: list[str],
        by: str,
        role: str,
        timestamp: str | None = None,
        authorization_ref: str | None = None,
    ) -> Decision:
        self._require_role(role, {ROLE_ARBITRATOR})
        case = self.get_case(order_id)
        if matter in case.decided_matters:
            raise ImmutabilityError(f"事项 {matter} 已有生效裁定")
        known = all_referencable_ids(case)
        unknown = [r for r in evidence_refs if r not in known]
        if unknown:
            raise IntakeValidationError("引用了不存在的证据：" + ",".join(unknown))
        rv = self._registry.get(case.rule_version)
        available_rule_ids = {line.split(" ", 1)[0] for line in rv.rules}
        bad_rules = [r for r in rule_ids if r not in available_rule_ids]
        if bad_rules:
            raise IntakeValidationError("引用了该案件规则版本之外的规则编号：" + ",".join(bad_rules))
        if authorization_ref is not None:
            auth_ids = {e["event_id"] for e in case.evidence[AGENT_AUTHORIZATION]}
            if authorization_ref not in auth_ids:
                raise IntakeValidationError("authorization_ref 必须指向已归档的代理授权事件")
        when = timestamp or self._clock()
        did = _decision_id(order_id, matter)
        entry = self._append(
            DECISION_MADE,
            {
                "decision_id": did,
                "matter": matter,
                "ruling": ruling,
                "rule_ids": list(rule_ids),
                "evidence_refs": list(evidence_refs),
                "order_ref": case.order_ref,
                "authorization_ref": authorization_ref,
                "decided_at": when,
                "by": by,
                "order_id": order_id,
            },
            did,
            when,
            by,
        )
        apply_management(case, entry)
        return case.decisions[did]

    # ---- 状态与结案后推进（进入调解/诉讼/撤回后仍可继续） -----------------
    def transition(self, *, order_id: str, to: str, by: str, role: str, note: str = "") -> Entry:
        self._require_role(role, {ROLE_COORDINATOR, ROLE_ARBITRATOR})
        case = self.get_case(order_id)
        if to not in TERMINAL_STATUSES and to != "open":
            raise IntakeValidationError(f"不支持的案件状态：{to}")
        entry = self._append(
            CASE_STATUS,
            {"order_id": order_id, "status": to, "note": note},
            f"status-{order_id}-{to}-{len(self._journal) + 1}",
            self._clock(),
            by,
        )
        apply_management(case, entry)
        return entry

    def update_deadline(self, *, order_id: str, name: str, at: str, status: str, by: str, role: str) -> Entry:
        self._require_role(role, {ROLE_COORDINATOR})
        case = self.get_case(order_id)
        entry = self._append(
            DEADLINE_UPDATE,
            {"order_id": order_id, "name": name, "at": at, "status": status},
            f"deadline-{order_id}-{name}-{len(self._journal) + 1}",
            self._clock(),
            by,
        )
        apply_management(case, entry)
        return entry

    def send_notice(self, *, order_id: str, template: str, to: str, by: str, role: str, status: str = "sent") -> Entry:
        self._require_role(role, {ROLE_COORDINATOR})
        case = self.get_case(order_id)
        entry = self._append(
            NOTICE_SENT,
            {"order_id": order_id, "template": template, "to": to, "status": status},
            f"notice-{order_id}-{template}-{len(self._journal) + 1}",
            self._clock(),
            by,
        )
        apply_management(case, entry)
        return entry

    def update_fee(self, *, order_id: str, name: str, amount: float, currency: str, status: str, by: str, role: str) -> Entry:
        self._require_role(role, {ROLE_COORDINATOR})
        case = self.get_case(order_id)
        if not isinstance(amount, (int, float)) or amount < 0:
            raise IntakeValidationError("费用金额必须是非负数")
        entry = self._append(
            FEE_UPDATE,
            {"order_id": order_id, "name": name, "amount": amount, "currency": currency, "status": status},
            f"fee-{order_id}-{name}-{len(self._journal) + 1}",
            self._clock(),
            by,
        )
        apply_management(case, entry)
        return entry

    # ---- 授权查看原件 -----------------------------------------------------
    def grant_access(self, *, order_id: str, grantee_role: str, labels: list[str], by: str) -> Entry:
        """当事人向某角色授予原件标签级访问权（如允许调解员查看签名原件）。"""
        case = self.get_case(order_id)
        if by not in {case.parties.get("buyer"), case.parties.get("seller")}:
            raise AuthorizationError("只有本案当事人可以授权原件访问")
        if grantee_role not in _VALID_ROLES:
            raise AuthorizationError(f"未知角色：{grantee_role}")
        labels = list(labels)
        bad = [x for x in labels if x not in _GRANT_LABELS]
        if not labels or bad:
            raise IntakeValidationError(f"授权标签必须取自 {sorted(_GRANT_LABELS)}")
        entry = self._append(
            GRANT_ADDED,
            {"order_id": order_id, "grantee_role": grantee_role, "labels": labels},
            f"grant-{order_id}-{grantee_role}-{len(self._journal) + 1}",
            self._clock(),
            by,
        )
        apply_management(case, entry)
        return entry

    def access_original(
        self,
        *,
        order_id: str,
        docs: list[str],
        actor: str,
        role: str,
        reason: str,
        destination: str | None = None,
    ) -> dict:
        """查看或跨境转交原件，每次都写审计；无原因、无授权一律拒绝。"""
        if role not in _VALID_ROLES:
            raise AuthorizationError(f"未知角色：{role}")
        if not reason or not reason.strip():
            raise AuthorizationError("访问原件必须填写原因")
        if not docs:
            raise IntakeValidationError("至少指定一份原件")
        case = self.get_case(order_id)
        if role == ROLE_OPERATOR:
            raise AuthorizationError("平台运营者只能补充交易事实，无权查看当事人原件")
        available = original_docs(case)
        missing = [d for d in docs if d not in available]
        if missing:
            raise AuthorizationError("请求的原件不存在：" + ",".join(missing))
        if role == ROLE_MEDIATOR:
            granted = self._authorized_labels(case, role)
            unlicensed = [d for d in docs if available[d] not in granted]
            if unlicensed:
                raise AuthorizationError("未获授权查看原件：" + ",".join(unlicensed))
        # party/coordinator/arbitrator 可在案内查看原件，但每次访问同样写审计；
        # operator 已在上方拒绝。跨境转交无论角色都再过一次同意覆盖校验。

        event_type = DATA_TRANSFER if destination is not None else DATA_ACCESS
        legal_basis = "case-administration"
        if destination is not None:
            self._check_cross_border(case, docs, available, destination)
            if any(available[d] in {"data_consent", "payment", "signature", "agent_auth"} for d in docs):
                legal_basis = "consent"
        entry = self._append(
            event_type,
            {
                "order_id": order_id,
                "docs": list(docs),
                "reason": reason,
                "destination": destination,
                "legal_basis": legal_basis,
            },
            f"{'xfer' if destination else 'access'}-{order_id}-{len(self._journal) + 1}",
            self._clock(),
            actor,
        )
        apply_management(case, entry)
        return {"audit_seq": entry.seq, "action": "transfer" if destination else "access", "legal_basis": legal_basis}

    def _check_cross_border(self, case: Case, docs: list[str], available: dict, destination: str) -> None:
        """跨境转交：已授予的数据处理同意必须同时覆盖接收方与数据范围。"""
        consents = [e["payload"] for e in case.evidence[DATA_CONSENT] if e["payload"].get("granted")]
        if not consents:
            raise AuthorizationError("跨境转交个人数据缺少数据处理同意")
        for doc in docs:
            label = available[doc]
            covered = any(
                destination in c.get("destinations", [])
                and (label in c.get("scopes", []) or "all" in c.get("scopes", []))
                for c in consents
            )
            if not covered:
                raise AuthorizationError(
                    f"数据处理同意未覆盖向 {destination} 转交 {doc}（接收方或数据范围不足）"
                )

    def _authorized_labels(self, case: Case, role: str) -> set[str]:
        labels: set[str] = set()
        # 调解员可见性来自两条合法依据，并严格限定到数据范围：
        # 1) 当事人通过 grant_access 显式授予的标签；
        # 2) 已授予且目的包含“调解”的数据处理同意所覆盖的范围标签。
        for grant in case.explicit_grants:
            if grant["grantee_role"] == role:
                labels.update(grant["labels"])
        for item in case.evidence[DATA_CONSENT]:
            c = item["payload"]
            if not c.get("granted"):
                continue
            if any("调解" in purpose for purpose in c.get("purposes", [])):
                labels.update(s for s in c.get("scopes", []) if s in _GRANT_LABELS)
        return labels

    # ---- 平台运营者补充事实；当事人陈述只追加 -----------------------------
    def add_operator_fact(self, event: dict, *, actor: str, role: str) -> Entry:
        self._require_role(role, {ROLE_OPERATOR})
        inc = validate_event(event)
        if inc.event_type != OPERATOR_FACT:
            raise IntakeValidationError("平台运营者只能通过 operator_fact 补充交易事实")
        case = self.get_case(inc.payload["order_id"])
        entry = self._append(OPERATOR_FACT, inc.payload, inc.event_id, inc.timestamp, actor)
        apply_business(case, entry)
        self._order_by_event[inc.event_id] = case.order_id
        return entry

    def add_statement(self, event: dict, *, actor: str, role: str) -> Entry:
        self._require_role(role, {ROLE_PARTY})
        inc = validate_event(event)
        if inc.event_type != PARTY_STATEMENT:
            raise IntakeValidationError("当事人只能提交 party_statement")
        case = self.get_case(inc.payload["order_id"])
        if actor not in {case.parties.get("buyer"), case.parties.get("seller")}:
            raise AuthorizationError("只有本案买卖当事人可以提交陈述")
        entry = self._append(PARTY_STATEMENT, inc.payload, inc.event_id, inc.timestamp, actor)
        apply_business(case, entry)
        self._order_by_event[inc.event_id] = case.order_id
        return entry

    def _require_role(self, role: str, allowed: set[str]) -> None:
        if role not in _VALID_ROLES:
            raise AuthorizationError(f"未知角色：{role}")
        if role not in allowed:
            raise AuthorizationError(f"角色 {role} 无权执行此操作，允许：{','.join(sorted(allowed))}")


def all_referencable_ids(case: Case) -> set[str]:
    """裁定可以引用的全部材料编号：各类证据、陈述、运营事实、补充协议。"""
    ids: set[str] = set()
    for items in case.evidence.values():
        ids.update(item["event_id"] for item in items)
    ids.update(item["event_id"] for item in case.statements)
    ids.update(item["event_id"] for item in case.operator_facts)
    for bucket in case.supplemental.values():
        ids.update(item["event_id"] for item in bucket)
    return ids


def original_docs(case: Case) -> dict[str, str]:
    """可作为“原件”访问的证据事件编号 -> 授权标签。"""
    docs: dict[str, str] = {}
    for etype, label in _DOC_GRANTS.items():
        for item in case.evidence.get(etype, []):
            docs[item["event_id"]] = label
    return docs
