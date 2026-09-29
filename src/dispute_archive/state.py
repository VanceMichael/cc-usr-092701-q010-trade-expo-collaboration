"""案件投影状态：由日志条目确定性重放得到，本身不落盘。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .events import (
    AGENT_AUTHORIZATION,
    DATA_CONSENT,
    JURISDICTION_AGREEMENT,
    OPERATOR_FACT,
    ORDER_EVENT,
    PARTY_STATEMENT,
    PAYMENT_RECORD,
    SIGNATURE_EVIDENCE,
    SUPPLEMENTAL_AGREEMENT,
    _PAID_STATUS,
    _UNPAID_STATUS,
)
from .journal import Entry

ROUTE_MEDIATION = "mediation"
ROUTE_MANUAL = "manual_review"

STATUS_OPEN = "open"
STATUS_MEDIATION = "mediation"
STATUS_LITIGATION = "litigation"
STATUS_WITHDRAWN = "withdrawn"


@dataclass
class Decision:
    decision_id: str
    matter: str
    ruling: str
    rule_ids: list[str]
    evidence_refs: list[str]
    order_ref: str
    authorization_ref: str | None
    decided_at: str
    by: str
    entry_seq: int


@dataclass
class Case:
    case_id: str
    order_id: str
    rule_version: str | None = None
    order_ref: str | None = None  # 订单事件编号（代理行为溯源锚点）
    occurred_at: str | None = None
    parties: dict[str, str] = field(default_factory=dict)
    status: str = STATUS_OPEN
    frozen: bool = False
    route: str | None = None
    freeze_reasons: list[str] = field(default_factory=list)
    emitted_reasons: set[str] = field(default_factory=set)
    evidence: dict[str, list[dict]] = field(
        default_factory=lambda: {
            SIGNATURE_EVIDENCE: [],
            PAYMENT_RECORD: [],
            DATA_CONSENT: [],
            AGENT_AUTHORIZATION: [],
            JURISDICTION_AGREEMENT: [],
            ORDER_EVENT: [],
        }
    )
    statements: list[dict] = field(default_factory=list)
    operator_facts: list[dict] = field(default_factory=list)
    supplemental: dict[str, list[dict]] = field(default_factory=dict)
    decisions: dict[str, Decision] = field(default_factory=dict)
    decided_matters: dict[str, str] = field(default_factory=dict)
    locked_evidence: set[str] = field(default_factory=set)
    deadlines: dict[str, dict] = field(default_factory=dict)
    notices: list[dict] = field(default_factory=list)
    fees: dict[str, dict] = field(default_factory=dict)
    audit: list[dict] = field(default_factory=list)
    status_history: list[dict] = field(default_factory=list)
    explicit_grants: list[dict] = field(default_factory=list)
    submitted_by: dict[str, str] = field(default_factory=dict)


def _ref(entry: Entry) -> dict[str, Any]:
    return {"seq": entry.seq, "event_id": entry.event_id, "entry_hash": entry.entry_hash}


def _signature_conflict(case: Case) -> str | None:
    items = case.evidence[SIGNATURE_EVIDENCE]
    by_doc: dict[str, list[dict]] = {}
    for item in items:
        by_doc.setdefault(item["payload"]["doc_ref"], []).append(item)
    for doc_ref, docs in by_doc.items():
        statuses = {d["payload"]["status"] for d in docs}
        if "valid" in statuses and (statuses & {"invalid", "mismatch"}):
            return f"signature:{doc_ref}"
        valid_signers = {d["payload"].get("signer") for d in docs if d["payload"]["status"] == "valid"}
        if len(valid_signers) > 1:
            return f"signature-signer:{doc_ref}"
    return None


def _payment_conflict(case: Case) -> str | None:
    items = case.evidence[PAYMENT_RECORD]
    polarity = {i["payload"]["status"] in _PAID_STATUS for i in items if i["payload"]["status"] in _PAID_STATUS | _UNPAID_STATUS}
    if True in polarity and False in polarity:
        return "payment:polarity"
    paid = [i for i in items if i["payload"]["status"] in _PAID_STATUS]
    by_institution = {i["payload"]["institution"]: i["payload"]["amount"] for i in paid}
    amounts = set(by_institution.values())
    if len(by_institution) >= 2 and len(amounts) >= 2:
        return "payment:amount"
    return None


def _jurisdiction_conflict(case: Case) -> str | None:
    forums = {i["payload"]["forum"] for i in case.evidence[JURISDICTION_AGREEMENT]}
    return "jurisdiction:forum" if len(forums) > 1 else None


def evaluate_routing(case: Case) -> list[dict[str, Any]]:
    """根据当前证据计算应追加的派生效果（冻结或自动分流）。确定性、无副作用。"""
    if case.order_ref is None:
        return []
    effects: list[dict[str, Any]] = []
    checks = (
        ("signature", _signature_conflict(case)),
        ("payment", _payment_conflict(case)),
        ("jurisdiction", _jurisdiction_conflict(case)),
    )
    new_reasons = [code for _kind, code in checks if code and code not in case.emitted_reasons]
    if new_reasons:
        effects.append({"kind": "frozen", "reasons": new_reasons})
    # 冻结具有黏性：一旦冻结，即使后续补齐材料也不恢复自动分流。
    if not case.frozen and not new_reasons:
        signatures = case.evidence[SIGNATURE_EVIDENCE]
        payments = case.evidence[PAYMENT_RECORD]
        forums = {i["payload"]["forum"] for i in case.evidence[JURISDICTION_AGREEMENT]}
        # 三类关键材料到齐前保持待定，不产生分流记录。
        if not (signatures and payments and forums):
            return effects
        has_valid_sig = any(i["payload"]["status"] == "valid" for i in signatures)
        has_paid = any(i["payload"]["status"] in _PAID_STATUS for i in payments)
        if has_valid_sig and has_paid and len(forums) == 1:
            effects.append({"kind": "routed", "route": ROUTE_MEDIATION})
        else:
            effects.append({"kind": "routed", "route": ROUTE_MANUAL})
    return effects


def apply_business(case: Case, entry: Entry) -> None:
    p = entry.payload
    ref = _ref(entry)
    et = entry.event_type
    case.submitted_by[entry.event_id] = entry.actor
    if et == ORDER_EVENT:
        case.occurred_at = p["occurred_at"]
        case.order_ref = entry.event_id
        case.parties.update({"buyer": p["buyer"], "seller": p["seller"]})
        case.evidence[ORDER_EVENT].append({**ref, "payload": p})
    elif et in case.evidence:
        case.evidence[et].append({**ref, "payload": p})
    elif et == PARTY_STATEMENT:
        # 陈述只追加，任何角色都没有改写入口。
        case.statements.append({**ref, "payload": p})
    elif et == OPERATOR_FACT:
        case.operator_facts.append({**ref, "payload": p})
    elif et == SUPPLEMENTAL_AGREEMENT:
        for change in p["changes"]:
            case.supplemental.setdefault(change["matter"], []).append({**ref, "change": change})


def apply_derived(case: Case, entry: Entry) -> None:
    p = entry.payload
    if p.get("kind") == "frozen":
        case.frozen = True
        for code in p["reasons"]:
            if code not in case.emitted_reasons:
                case.freeze_reasons.append(code)
                case.emitted_reasons.add(code)
        case.route = None
    elif p.get("kind") == "routed":
        case.route = p["route"]


def apply_management(case: Case, entry: Entry) -> None:
    """重放服务自身生成的管理事件。"""
    from .events import (
        AUTO_FROZEN,
        CASE_OPENED,
        CASE_STATUS,
        DATA_ACCESS,
        DATA_TRANSFER,
        DECISION_MADE,
        DEADLINE_UPDATE,
        FEE_UPDATE,
        GRANT_ADDED,
        NOTICE_SENT,
        ROUTING_DECIDED,
    )

    et = entry.event_type
    p = entry.payload
    if et == CASE_OPENED:
        case.rule_version = p["rule_version"]
        if case.occurred_at is None and p.get("occurred_at"):
            case.occurred_at = p["occurred_at"]
    elif et in (ROUTING_DECIDED, AUTO_FROZEN):
        apply_derived(case, entry)
    elif et == DECISION_MADE:
        decision = Decision(
            decision_id=p["decision_id"],
            matter=p["matter"],
            ruling=p["ruling"],
            rule_ids=list(p["rule_ids"]),
            evidence_refs=list(p["evidence_refs"]),
            order_ref=p["order_ref"],
            authorization_ref=p.get("authorization_ref"),
            decided_at=p["decided_at"],
            by=p["by"],
            entry_seq=entry.seq,
        )
        case.decisions[decision.decision_id] = decision
        case.decided_matters[decision.matter] = decision.decision_id
        case.locked_evidence.update(decision.evidence_refs)
    elif et == CASE_STATUS:
        case.status = p["status"]
        case.status_history.append({"seq": entry.seq, "at": entry.timestamp, "status": p["status"], "note": p.get("note", "")})
    elif et == DEADLINE_UPDATE:
        case.deadlines[p["name"]] = {"at": p["at"], "status": p["status"], "updated_seq": entry.seq}
    elif et == NOTICE_SENT:
        case.notices.append({"seq": entry.seq, "at": entry.timestamp, "template": p["template"], "to": p["to"], "status": p.get("status", "sent")})
    elif et == FEE_UPDATE:
        case.fees[p["name"]] = {"amount": p["amount"], "currency": p["currency"], "status": p["status"], "updated_seq": entry.seq}
    elif et in (DATA_ACCESS, DATA_TRANSFER):
        case.audit.append(
            {
                "seq": entry.seq,
                "at": entry.timestamp,
                "action": "transfer" if et == DATA_TRANSFER else "access",
                "actor": entry.actor,
                "docs": list(p["docs"]),
                "reason": p["reason"],
                "destination": p.get("destination"),
                "legal_basis": p.get("legal_basis"),
                "order_id": p.get("order_id"),
            }
        )
    elif et == GRANT_ADDED:
        case.explicit_grants.append(
            {
                "seq": entry.seq,
                "grantee_role": p["grantee_role"],
                "labels": list(p["labels"]),
                "by": entry.actor,
            }
        )


def rebuild(case_id: str, order_id: str, entries: list[Entry]) -> Case:
    """按日志条目重建案件状态；业务事件先入投影，派生/管理事件随之生效。"""
    from .events import BUSINESS_TYPES

    case = Case(case_id=case_id, order_id=order_id)
    for entry in entries:
        if entry.event_type in BUSINESS_TYPES:
            apply_business(case, entry)
        else:
            apply_management(case, entry)
    return case
