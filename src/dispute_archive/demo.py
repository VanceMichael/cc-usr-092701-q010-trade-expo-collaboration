"""端到端演示：用虚构数据走查归档、冻结、授权、跨境审计、裁定与恢复溯源。"""

from __future__ import annotations

import json
from pathlib import Path

from .errors import AuthorizationError, ImmutabilityError, RoutingBlocked
from .package import build_package, package_json, trace_decision
from .rules import build_default_registry
from .service import (
    ROLE_ARBITRATOR,
    ROLE_COORDINATOR,
    ROLE_MEDIATOR,
    ROLE_OPERATOR,
    ROLE_PARTY,
    DisputeService,
)


def run_demo(fixture_path: Path) -> tuple[DisputeService, dict]:
    data = json.loads(fixture_path.read_text(encoding="utf-8"))
    service = DisputeService(build_default_registry())
    events_by_order = {c["order_id"]: list(c["events"]) for c in data["cases"]}
    results = {}

    # ---- 归档（含一次重复提交，应返回原案件） ----
    for case_data in data["cases"]:
        for event in case_data["events"]:
            r = service.submit(event, actor="intake-bot")
            results[event["event_id"]] = r
    first = data["cases"][0]["events"][0]
    duplicate = service.submit(dict(first), actor="intake-bot-retry")

    oid_a = data["cases"][0]["order_id"]
    oid_b = data["cases"][1]["order_id"]
    case_a = service.get_case(oid_a)
    case_b = service.get_case(oid_b)

    # ---- 角色与授权：调解员查看获授权原件并跨境转交 ----
    sig_id = next(e["event_id"] for e in events_by_order[oid_a] if e["event_type"] == "signature_evidence")
    pay_id = next(e["event_id"] for e in events_by_order[oid_a] if e["event_type"] == "payment_record")
    auth_id = next(e["event_id"] for e in events_by_order[oid_a] if e["event_type"] == "agent_authorization")

    # 无原因访问被拒绝（先证明审计门槛存在）
    access_without_reason = "rejected: reason required"
    try:
        service.access_original(order_id=oid_a, docs=[sig_id], actor="调解员-林某", role=ROLE_MEDIATOR, reason="")
    except AuthorizationError:
        access_without_reason = "rejected: reason required"

    access = service.access_original(
        order_id=oid_a, docs=[sig_id], actor="调解员-林某", role=ROLE_MEDIATOR, reason="核验合同是否成立"
    )
    transfer = service.access_original(
        order_id=oid_a,
        docs=[pay_id, sig_id, auth_id],
        actor="协调员-陈某",
        role=ROLE_COORDINATOR,
        reason="应新加坡调解中心请求移交付款与签署原件",
        destination="SG-Mediation-Centre",
    )

    # 运营者无权看原件
    operator_denied = "rejected"
    try:
        service.access_original(order_id=oid_a, docs=[pay_id], actor="平台值班-赵某", role=ROLE_OPERATOR, reason="排查")
    except AuthorizationError:
        operator_denied = "rejected: operator cannot view originals"

    # 当事人追加陈述；运营者补充事实（二者分离）
    stmt_event = {
        "event_id": "evt-stmt-0912-b",
        "event_type": "party_statement",
        "timestamp": "2025-09-13T03:00:00Z",
        "payload": {"order_id": oid_a, "party": case_a.parties["buyer"], "text": "我方确认智能助手授权额度覆盖本笔订单。"},
    }
    service.add_statement(stmt_event, actor=case_a.parties["buyer"], role=ROLE_PARTY)
    fact_event = {
        "event_id": "evt-fact-0912-b",
        "event_type": "operator_fact",
        "timestamp": "2025-09-13T03:05:00Z",
        "payload": {"order_id": oid_a, "text": "平台风控复核：该账号下单设备与历史常用设备一致。"},
    }
    service.add_operator_fact(fact_event, actor="平台值班-赵某", role=ROLE_OPERATOR)

    # ---- 裁定：合同成立，引用代理授权/签名/支付/订单 ----
    decision = service.make_decision(
        order_id=oid_a,
        matter="合同是否成立",
        ruling="智能助手在授权范围内下单，电子签名可靠且付款已到账，合同于订单到达卖方系统时成立。",
        rule_ids=["R1", "R2", "R3"],
        evidence_refs=["evt-ord-0912", auth_id, sig_id, pay_id],
        authorization_ref=auth_id,
        by="裁定人-沈某",
        role=ROLE_ARBITRATOR,
        timestamp="2025-09-13T06:00:00Z",
    )

    # ---- 补充协议边界 ----
    supplemental_ok = service.add_supplemental(
        {
            "event_id": "evt-sup-0912-a",
            "event_type": "supplemental_agreement",
            "timestamp": "2025-09-13T07:00:00Z",
            "payload": {"order_id": oid_a, "changes": [{"matter": "交付方式", "content": "授权包改为分两批交付"}]},
        },
        actor=case_a.parties["buyer"],
    )
    supplemental_rejected = "rejected"
    try:
        service.add_supplemental(
            {
                "event_id": "evt-sup-0912-bad",
                "event_type": "supplemental_agreement",
                "timestamp": "2025-09-13T07:10:00Z",
                "payload": {
                    "order_id": oid_a,
                    "changes": [{"matter": "合同是否成立", "content": "双方事后约定视为不成立"}],
                },
            }
        )
    except ImmutabilityError as exc:
        supplemental_rejected = f"rejected: {exc}"

    # ---- 进入调解后，期限/通知/费用继续推进 ----
    service.transition(order_id=oid_a, to="mediation", by="协调员-陈某", role=ROLE_COORDINATOR, note="双方同意调解")
    service.update_deadline(order_id=oid_a, name="举证期限", at="2025-09-20T00:00:00Z", status="open", by="协调员-陈某", role=ROLE_COORDINATOR)
    service.send_notice(order_id=oid_a, template="mediation-session", to=case_a.parties["seller"], by="协调员-陈某", role=ROLE_COORDINATOR)
    service.update_fee(order_id=oid_a, name="调解费", amount=50.0, currency="USD", status="payable", by="协调员-陈某", role=ROLE_COORDINATOR)
    service.update_fee(order_id=oid_a, name="调解费", amount=50.0, currency="USD", status="paid", by="协调员-陈某", role=ROLE_COORDINATOR)

    # ---- 矛盾案件：自动分流冻结 ----
    frozen_route = "blocked"
    try:
        service.require_routing(oid_b)
    except RoutingBlocked as exc:
        frozen_route = f"blocked: {','.join(exc.reasons)}"

    # ---- 装订争议包、溯源，导出日志 ----
    package = build_package(service, oid_a)
    trace = trace_decision(service, oid_a, decision.decision_id)
    log_text = service.export_log()

    # ---- 系统恢复：仅靠日志重建，并复核链与溯源 ----
    recovered = DisputeService.recover(log_text, build_default_registry())
    recovered_trace = trace_decision(recovered, oid_a, decision.decision_id)
    recovered_package = build_package(recovered, oid_a)

    summary = {
        "case_a": {
            "case_id": case_a.case_id,
            "rule_version": case_a.rule_version,
            "route": case_a.route,
            "frozen": case_a.frozen,
            "duplicate_event_returns_same_case": duplicate.duplicate and duplicate.case_id == case_a.case_id,
            "access_without_reason": access_without_reason,
            "mediator_access_seq": access["audit_seq"],
            "cross_border_transfer_seq": transfer["audit_seq"],
            "operator_denied": operator_denied,
            "decision_id": decision.decision_id,
            "locked_evidence_count": len(case_a.locked_evidence),
            "supplemental_ok_seq": supplemental_ok.seq,
            "supplemental_decided_matter": supplemental_rejected,
            "status": case_a.status,
            "deadline": case_a.deadlines.get("举证期限"),
            "notices": len(case_a.notices),
            "fee_status": case_a.fees["调解费"]["status"],
            "audit_events": len(case_a.audit),
        },
        "case_b": {
            "case_id": case_b.case_id,
            "rule_version": case_b.rule_version,
            "frozen": case_b.frozen,
            "freeze_reasons": list(case_b.freeze_reasons),
            "require_routing": frozen_route,
        },
        "recovery": {
            "entry_count": len(recovered.journal),
            "trace_evidences_match": [e["event_id"] for e in recovered_trace["evidence_chain"]]
            == [e["event_id"] for e in trace["evidence_chain"]],
            "package_digest_match": recovered_package["package_digest"] == package["package_digest"],
        },
        "package_digest": package["package_digest"],
        "trace_digest": trace["trace_digest"],
    }
    return service, summary


def main(fixture_path: Path, *, out_package: Path | None = None, out_log: Path | None = None) -> None:
    service, summary = run_demo(fixture_path)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    if out_package is not None:
        first_order = json.loads(fixture_path.read_text(encoding="utf-8"))["cases"][0]["order_id"]
        out_package.write_text(package_json(build_package(service, first_order)), encoding="utf-8")
    if out_log is not None:
        out_log.write_text(service.export_log(), encoding="utf-8")
