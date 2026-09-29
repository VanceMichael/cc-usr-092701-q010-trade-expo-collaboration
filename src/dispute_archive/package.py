"""可复核争议包与裁定溯源。

争议包把案件对应的日志片段、适用规则全文、证据、裁定和审计装订成一个自带
摘要的只读对象；包摘要对内容取 SHA-256，任何改动都会改变摘要，便于复核。
"""

from __future__ import annotations

from typing import Any

from .journal import digest
from .service import DisputeService
from .state import Case


def _case_entries(service: DisputeService, order_id: str) -> list:
    return [e for e in service.journal if e.payload.get("order_id") == order_id]


def _rule_basis(service: DisputeService, case: Case) -> list[dict[str, str]]:
    rv = service.registry.get(case.rule_version)
    return [
        {"id": line.split(" ", 1)[0], "text": line, "version": rv.version}
        for line in rv.rules
    ]


def build_package(service: DisputeService, order_id: str) -> dict[str, Any]:
    """生成可复核争议包（dict），含包摘要与日志链锚点。"""
    case = service.get_case(order_id)
    entries = _case_entries(service, order_id)
    rv = service.registry.get(case.rule_version)

    evidence = {
        kind: [{"seq": e["seq"], "event_id": e["event_id"], "entry_hash": e["entry_hash"], "payload": e["payload"]} for e in items]
        for kind, items in case.evidence.items()
        if items
    }
    package: dict[str, Any] = {
        "package_format": "dispute-package/1",
        "case": {
            "case_id": case.case_id,
            "order_id": case.order_id,
            "occurred_at": case.occurred_at,
            "parties": case.parties,
            "status": case.status,
            "route": case.route,
            "frozen": case.frozen,
            "freeze_reasons": list(case.freeze_reasons),
        },
        "rule_version": {
            "version": rv.version,
            "effective_from": rv.effective_from,
            "effective_until": rv.effective_until,
            "notes": rv.notes,
            "rules": list(rv.rules),
        },
        "evidence": evidence,
        "statements": [
            {"seq": s["seq"], "event_id": s["event_id"], "entry_hash": s["entry_hash"], "payload": s["payload"]}
            for s in case.statements
        ],
        "operator_facts": [
            {"seq": f["seq"], "event_id": f["event_id"], "entry_hash": f["entry_hash"], "payload": f["payload"]}
            for f in case.operator_facts
        ],
        "supplemental_agreements": [
            {"seq": item["seq"], "event_id": item["event_id"], "entry_hash": item["entry_hash"], "change": item["change"]}
            for bucket in case.supplemental.values()
            for item in bucket
        ],
        "decisions": [
            {
                "decision_id": d.decision_id,
                "matter": d.matter,
                "ruling": d.ruling,
                "rule_ids": list(d.rule_ids),
                "evidence_refs": list(d.evidence_refs),
                "order_ref": d.order_ref,
                "authorization_ref": d.authorization_ref,
                "decided_at": d.decided_at,
                "by": d.by,
                "entry_seq": d.entry_seq,
            }
            for d in case.decisions.values()
        ],
        "lifecycle": {
            "history": list(case.status_history),
            "deadlines": dict(case.deadlines),
            "notices": list(case.notices),
            "fees": dict(case.fees),
        },
        "access_audit": list(case.audit),
        "chain": {
            "first_seq": entries[0].seq if entries else None,
            "last_seq": entries[-1].seq if entries else None,
            "anchors": [
                {"seq": e.seq, "event_type": e.event_type, "event_id": e.event_id, "prev_hash": e.prev_hash, "entry_hash": e.entry_hash}
                for e in entries
            ],
            "journal_head": service.journal.head,
        },
    }
    package["package_digest"] = digest(package)
    return package


def trace_decision(service: DisputeService, order_id: str, decision_id: str | None = None, *, matter: str | None = None) -> dict[str, Any]:
    """从一项裁定追溯：代理行为 → 规则依据 → 完整证据链。

    可按 ``decision_id`` 或 ``matter`` 指定裁定。
    """
    case = service.get_case(order_id)
    if decision_id is None:
        if matter is None or matter not in case.decided_matters:
            raise KeyError(f"找不到该事项的裁定：{matter}")
        decision_id = case.decided_matters[matter]
    decision = case.decisions.get(decision_id)
    if decision is None:
        raise KeyError(f"找不到裁定：{decision_id}")

    rule_texts = {line.split(" ", 1)[0]: line for line in service.registry.get(case.rule_version).rules}
    rule_basis = [{"id": rid, "text": rule_texts.get(rid, ""), "version": case.rule_version} for rid in decision.rule_ids]

    # 触发裁定的代理行为：订单事件（可能由智能助手代下）及其代理授权。
    order_events = case.evidence["order_event"]
    agency_trigger = None
    if order_events:
        order = order_events[0]
        agency_trigger = {
            "order_event_id": order["event_id"],
            "seq": order["seq"],
            "entry_hash": order["entry_hash"],
            "payload": order["payload"],
        }
    authorization = None
    if decision.authorization_ref:
        for auth in case.evidence["agent_authorization"]:
            if auth["event_id"] == decision.authorization_ref:
                authorization = {
                    "authorization_event_id": auth["event_id"],
                    "seq": auth["seq"],
                    "entry_hash": auth["entry_hash"],
                    "payload": auth["payload"],
                }
    else:
        for auth in case.evidence["agent_authorization"]:
            authorization = {
                "authorization_event_id": auth["event_id"],
                "seq": auth["seq"],
                "entry_hash": auth["entry_hash"],
                "payload": auth["payload"],
            }
            break

    # 完整证据链：裁定引用的每份材料 → 其日志位置与哈希。
    entries_by_id = {e.event_id: e for e in service.journal}
    evidence_chain = []
    for ref in decision.evidence_refs:
        entry = entries_by_id.get(ref)
        evidence_chain.append(
            {
                "event_id": ref,
                "seq": entry.seq if entry else None,
                "event_type": entry.event_type if entry else None,
                "entry_hash": entry.entry_hash if entry else None,
                "payload": entry.payload if entry else None,
                "locked_by_decision": ref in case.locked_evidence,
            }
        )

    # 裁定之前本案的完整日志轨迹（发生顺序 + 哈希前后继）。
    lead_up = [
        {
            "seq": e.seq,
            "timestamp": e.timestamp,
            "event_type": e.event_type,
            "event_id": e.event_id,
            "actor": e.actor,
            "prev_hash": e.prev_hash,
            "entry_hash": e.entry_hash,
        }
        for e in _case_entries(service, order_id)
        if e.seq <= decision.entry_seq
    ]

    return {
        "case_id": case.case_id,
        "order_id": order_id,
        "rule_version": case.rule_version,
        "decision": {
            "decision_id": decision.decision_id,
            "matter": decision.matter,
            "ruling": decision.ruling,
            "decided_at": decision.decided_at,
            "by": decision.by,
            "entry_seq": decision.entry_seq,
            "entry_hash": entries_by_id[decision.decision_id].entry_hash,
        },
        "agency_trigger": agency_trigger,
        "agent_authorization": authorization,
        "rule_basis": rule_basis,
        "evidence_chain": evidence_chain,
        "journal_lead_up": lead_up,
        "trace_digest": digest(
            {
                "decision_id": decision.decision_id,
                "agency": agency_trigger,
                "authorization": authorization,
                "rules": rule_basis,
                "evidence": evidence_chain,
            }
        ),
    }


def package_json(package: dict) -> str:
    import json

    return json.dumps(package, ensure_ascii=False, indent=2, sort_keys=True)
