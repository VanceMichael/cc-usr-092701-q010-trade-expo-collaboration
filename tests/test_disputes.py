"""争议归档与分流服务的单元测试。"""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from src.dispute_archive import (
    AuthorizationError,
    ChainIntegrityError,
    ImmutabilityError,
    IntakeValidationError,
    Journal,
    RoutingBlocked,
    RuleVersionError,
    build_default_registry,
    build_package,
    trace_decision,
)
from src.dispute_archive.service import (
    ROLE_ARBITRATOR,
    ROLE_COORDINATOR,
    ROLE_MEDIATOR,
    ROLE_OPERATOR,
    ROLE_PARTY,
    DisputeService,
)

FIXTURE = Path("fixtures/dispute_scenario.json")


def evt(event_id, etype, ts, **payload):
    return {"event_id": event_id, "event_type": etype, "timestamp": ts, "payload": payload}


def order(oid="ORD-T-1", when="2025-07-01T00:00:00Z", buyer="买方", seller="卖方", amount=100.0, currency="USD"):
    return evt(f"ord-{oid}", "order_event", when,
               order_id=oid, occurred_at=when, buyer=buyer, seller=seller,
               amount=amount, currency=currency)


def auth(oid="ORD-T-1", eid=None):
    return evt(eid or f"auth-{oid}", "agent_authorization", "2025-07-01T00:01:00Z",
               order_id=oid, grantor="买方", agent="采购助手",
               scope=["order"], valid_from="2025-01-01T00:00:00Z", valid_to="2026-01-01T00:00:00Z")


def signature(oid="ORD-T-1", eid=None, status="valid", doc=None):
    return evt(eid or f"sig-{oid}", "signature_evidence", "2025-07-01T00:02:00Z",
               order_id=oid, doc_ref=doc or f"contract-{oid}", signer="卖方", method="电子签名", status=status)


def consent(oid="ORD-T-1", eid=None, granted=True, destinations=("SG-MC",), scopes=("payment", "signature", "agent_auth"),
            purposes=("争议调解",)):
    return evt(eid or f"con-{oid}", "data_consent", "2025-07-01T00:02:30Z",
               order_id=oid, consent_id=f"C-{oid}", subject="买方联系人",
               purposes=list(purposes), destinations=list(destinations), scopes=list(scopes), granted=granted)


def payment(oid="ORD-T-1", eid=None, status="paid", institution="结算机构A", amount=100.0):
    return evt(eid or f"pay-{oid}", "payment_record", "2025-07-01T00:03:00Z",
               order_id=oid, institution=institution, status=status, amount=amount, currency="USD")


def jurisdiction(oid="ORD-T-1", eid=None, forum="杭州国际商事调解中心"):
    return evt(eid or f"jur-{oid}", "jurisdiction_agreement", "2025-07-01T00:03:30Z",
               order_id=oid, forum=forum, governing_law="中国法")


def statement(oid="ORD-T-1", eid=None, text="陈述内容", party="买方"):
    return evt(eid or f"stmt-{oid}", "party_statement", "2025-07-02T00:00:00Z",
               order_id=oid, party=party, text=text)


def fact(oid="ORD-T-1", eid=None, text="平台事实"):
    return evt(eid or f"fact-{oid}", "operator_fact", "2025-07-02T00:05:00Z", order_id=oid, text=text)


def seed(service, *events, actor="intake"):
    return [service.submit(e, actor=actor) for e in events]


class IntakeTest(unittest.TestCase):
    def setUp(self):
        self.svc = DisputeService(build_default_registry())

    def test_order_picks_rule_version_in_force_at_occurrence(self):
        r = self.svc.submit(order("ORD-OLD", when="2024-05-01T00:00:00Z"))
        self.assertEqual(r.rule_version, "DTE-2024.1")
        svc2 = DisputeService(build_default_registry())
        r2 = svc2.submit(order("ORD-NEW", when="2026-05-01T00:00:00Z"))
        self.assertEqual(r2.rule_version, "DTE-2026.1")
        # 2024 版边界：生效日当天即适用
        svc3 = DisputeService(build_default_registry())
        r3 = svc3.submit(order("ORD-EDGE", when="2024-01-01T00:00:00Z"))
        self.assertEqual(r3.rule_version, "DTE-2024.1")

    def test_missing_rule_version(self):
        with self.assertRaises(RuleVersionError):
            self.svc.submit(order("ORD-NO-RULE", when="2023-12-31T23:59:59Z"))

    def test_evidence_before_order_rejected(self):
        with self.assertRaisesRegex(IntakeValidationError, "订单事件尚未归档"):
            self.svc.submit(signature())

    def test_bad_payload(self):
        with self.assertRaisesRegex(IntakeValidationError, "金额"):
            self.svc.submit(evt("x", "order_event", "t", order_id="Z", occurred_at="2025-01-01T00:00:00Z",
                                buyer="b", seller="s", amount=0, currency="USD"))
        with self.assertRaisesRegex(IntakeValidationError, "签名状态"):
            seed(self.svc, order())
            self.svc.submit(signature(status="forged"))

    def test_case_id_stable_for_order(self):
        r1 = self.svc.submit(order())
        r2 = self.svc.submit(signature())
        self.assertEqual(r1.case_id, r2.case_id)
        self.assertTrue(r1.created)
        self.assertFalse(r2.created)


class IdempotencyTest(unittest.TestCase):
    def setUp(self):
        self.svc = DisputeService(build_default_registry())

    def test_duplicate_event_returns_original_case(self):
        r1 = self.svc.submit(order())
        before = len(self.svc.journal)
        r2 = self.svc.submit(copy.deepcopy(order()))
        self.assertTrue(r2.duplicate)
        self.assertEqual(r2.case_id, r1.case_id)
        self.assertEqual(len(self.svc.journal), before)

    def test_distinct_events_same_order_append(self):
        seed(self.svc, order())
        r = self.svc.submit(signature())
        self.assertFalse(r.duplicate)


class RoutingTest(unittest.TestCase):
    def setUp(self):
        self.svc = DisputeService(build_default_registry())

    def test_consistent_materials_route_to_mediation(self):
        seed(self.svc, order(), auth(), signature(), payment(), jurisdiction())
        self.assertEqual(self.svc.require_routing("ORD-T-1"), "mediation")

    def test_incomplete_materials_pending(self):
        seed(self.svc, order(), signature())
        self.assertEqual(self.svc.require_routing("ORD-T-1"), "pending")

    def test_conflicting_signatures_freeze(self):
        seed(self.svc, order(),
             signature(eid="sig-a", status="valid"),
             signature(eid="sig-b", status="mismatch"))
        case = self.svc.get_case("ORD-T-1")
        self.assertTrue(case.frozen)
        self.assertIn("signature:contract-ORD-T-1", case.freeze_reasons)
        with self.assertRaises(RoutingBlocked) as cm:
            self.svc.require_routing("ORD-T-1")
        self.assertEqual(cm.exception.code, "frozen")

    def test_conflicting_payment_polarity_freeze(self):
        seed(self.svc, order(), signature(),
             payment(eid="pay-a", status="paid", institution="机构A"),
             payment(eid="pay-b", status="failed", institution="机构B"))
        self.assertTrue(self.svc.get_case("ORD-T-1").frozen)

    def test_conflicting_jurisdiction_freeze(self):
        seed(self.svc, order(), signature(), payment(),
             jurisdiction(eid="j1", forum="杭州"),
             jurisdiction(eid="j2", forum="新加坡"))
        self.assertIn("jurisdiction:forum", self.svc.get_case("ORD-T-1").freeze_reasons)

    def test_freeze_is_sticky(self):
        seed(self.svc, order(),
             signature(eid="sig-a", status="valid"),
             signature(eid="sig-b", status="invalid"),
             payment(), jurisdiction())
        self.assertTrue(self.svc.get_case("ORD-T-1").frozen)
        # 即使之后补交新的有效签名与正常支付，也不恢复自动分流
        self.svc.submit(signature(eid="sig-c", status="valid"))
        self.svc.submit(payment(eid="pay-2", status="paid", institution="机构C"))
        self.assertTrue(self.svc.get_case("ORD-T-1").frozen)
        self.assertIsNone(self.svc.get_case("ORD-T-1").route)

    def test_non_conflicting_multiple_signatures_ok(self):
        seed(self.svc, order(),
             signature(eid="sig-a", status="valid", doc="contract-ORD-T-1"),
             signature(eid="sig-b", status="valid", doc="appendix-ORD-T-1"),
             payment(), jurisdiction())
        self.assertFalse(self.svc.get_case("ORD-T-1").frozen)


class RolesAndAccessTest(unittest.TestCase):
    def setUp(self):
        self.svc = DisputeService(build_default_registry())
        seed(self.svc, order("ORD-T-1", buyer="买方", seller="卖方"),
             auth(), signature(), consent(), payment(), jurisdiction())

    def test_mediator_needs_authorization_or_consent(self):
        # 同意目的含“争议调解”且范围含 signature，可看
        ok = self.svc.access_original(order_id="ORD-T-1", docs=["sig-ORD-T-1"],
                                      actor="调解员", role=ROLE_MEDIATOR, reason="核验签名")
        self.assertEqual(ok["action"], "access")
        # 无数据处理同意的标签（data_consent 未授权给调解）→ 拒绝
        with self.assertRaisesRegex(AuthorizationError, "未获授权"):
            self.svc.access_original(order_id="ORD-T-1", docs=["con-ORD-T-1"],
                                     actor="调解员", role=ROLE_MEDIATOR, reason="查看同意原件")

    def test_explicit_grant_by_party(self):
        svc = DisputeService(build_default_registry())
        seed(svc, order("ORD-X", buyer="买方B", seller="卖方S"), auth(oid="ORD-X"),
             signature(oid="ORD-X"), payment(oid="ORD-X"), jurisdiction(oid="ORD-X"))
        # 没有同意也没有授权 → 调解员看不了
        with self.assertRaises(AuthorizationError):
            svc.access_original(order_id="ORD-X", docs=[f"sig-ORD-X"], actor="调解员",
                                role=ROLE_MEDIATOR, reason="核验")
        svc.grant_access(order_id="ORD-X", grantee_role=ROLE_MEDIATOR, labels=["signature"], by="买方B")
        r = svc.access_original(order_id="ORD-X", docs=[f"sig-ORD-X"], actor="调解员",
                                role=ROLE_MEDIATOR, reason="核验签名")
        self.assertEqual(r["action"], "access")
        # 非当事人不能授权
        with self.assertRaises(AuthorizationError):
            svc.grant_access(order_id="ORD-X", grantee_role=ROLE_MEDIATOR, labels=["payment"], by="路人")

    def test_access_requires_reason(self):
        with self.assertRaisesRegex(AuthorizationError, "原因"):
            self.svc.access_original(order_id="ORD-T-1", docs=["sig-ORD-T-1"],
                                     actor="调解员", role=ROLE_MEDIATOR, reason="  ")

    def test_operator_cannot_view_originals(self):
        with self.assertRaisesRegex(AuthorizationError, "平台运营者"):
            self.svc.access_original(order_id="ORD-T-1", docs=["pay-ORD-T-1"],
                                     actor="平台", role=ROLE_OPERATOR, reason="对账")

    def test_operator_may_add_facts_but_not_statements(self):
        self.svc.add_operator_fact(fact(), actor="平台值班", role=ROLE_OPERATOR)
        with self.assertRaises(AuthorizationError):
            self.svc.add_operator_fact(fact(), actor="平台", role=ROLE_PARTY)
        with self.assertRaises(AuthorizationError):
            self.svc.add_statement(statement(), actor="平台", role=ROLE_OPERATOR)
        # 运营者不能借补充事实接口改写陈述：陈述数量不被 fact 影响
        self.assertEqual(len(self.svc.get_case("ORD-T-1").statements), 0)
        self.assertEqual(len(self.svc.get_case("ORD-T-1").operator_facts), 1)

    def test_only_party_actor_may_statement(self):
        self.svc.add_statement(statement(party="买方"), actor="买方", role=ROLE_PARTY)
        with self.assertRaises(AuthorizationError):
            self.svc.add_statement(statement(eid="s2"), actor="不是当事人", role=ROLE_PARTY)
        # 陈述只追加：两份都在
        self.assertEqual(len(self.svc.get_case("ORD-T-1").statements), 1)

    def test_unknown_role(self):
        with self.assertRaises(AuthorizationError):
            self.svc.access_original(order_id="ORD-T-1", docs=["sig-ORD-T-1"],
                                     actor="x", role="auditor", reason="r")

    def test_every_access_audited_with_reason(self):
        self.svc.access_original(order_id="ORD-T-1", docs=["sig-ORD-T-1"],
                                 actor="调解员", role=ROLE_MEDIATOR, reason="核验合同")
        audit = self.svc.get_case("ORD-T-1").audit
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["reason"], "核验合同")
        self.assertEqual(audit[0]["actor"], "调解员")


class CrossBorderTest(unittest.TestCase):
    def setUp(self):
        self.svc = DisputeService(build_default_registry())

    def test_transfer_allowed_with_covering_consent(self):
        seed(self.svc, order(), auth(), signature(),
             consent(destinations=["SG-MC"], scopes=["payment", "signature"]),
             payment(), jurisdiction())
        r = self.svc.access_original(order_id="ORD-T-1", docs=["sig-ORD-T-1", "pay-ORD-T-1"],
                                     actor="协调员", role=ROLE_COORDINATOR,
                                     reason="应新方请求转交", destination="SG-MC")
        self.assertEqual(r["action"], "transfer")
        self.assertEqual(r["legal_basis"], "consent")
        audit = self.svc.get_case("ORD-T-1").audit[-1]
        self.assertEqual(audit["destination"], "SG-MC")

    def test_transfer_without_consent_denied(self):
        seed(self.svc, order(), signature(), payment(), jurisdiction())
        with self.assertRaisesRegex(AuthorizationError, "数据处理同意"):
            self.svc.access_original(order_id="ORD-T-1", docs=["pay-ORD-T-1"],
                                     actor="协调员", role=ROLE_COORDINATOR,
                                     reason="转交", destination="SG-MC")

    def test_transfer_destination_or_scope_not_covered(self):
        seed(self.svc, order(), signature(),
             consent(destinations=["SG-MC"], scopes=["signature"]),
             payment(), jurisdiction())
        with self.assertRaisesRegex(AuthorizationError, "接收方或数据范围不足"):
            self.svc.access_original(order_id="ORD-T-1", docs=["pay-ORD-T-1"],
                                     actor="协调员", role=ROLE_COORDINATOR,
                                     reason="转交付款流水", destination="SG-MC")
        with self.assertRaisesRegex(AuthorizationError, "接收方或数据范围不足"):
            self.svc.access_original(order_id="ORD-T-1", docs=["sig-ORD-T-1"],
                                     actor="协调员", role=ROLE_COORDINATOR,
                                     reason="转交", destination="DE-COURT")

    def test_revoked_consent_does_not_authorize(self):
        seed(self.svc, order(), signature(),
             consent(granted=False), payment(), jurisdiction())
        with self.assertRaises(AuthorizationError):
            self.svc.access_original(order_id="ORD-T-1", docs=["sig-ORD-T-1"],
                                     actor="协调员", role=ROLE_COORDINATOR,
                                     reason="转交", destination="SG-MC")


class SupplementalTest(unittest.TestCase):
    def setUp(self):
        self.svc = DisputeService(build_default_registry())
        seed(self.svc, order(), auth(), signature(), consent(), payment(), jurisdiction())

    def _decide(self, matter="合同是否成立"):
        return self.svc.make_decision(
            order_id="ORD-T-1", matter=matter, ruling="成立",
            rule_ids=["R1", "R2"], evidence_refs=["ord-ORD-T-1", "sig-ORD-T-1"],
            authorization_ref="auth-ORD-T-1", by="裁定人", role=ROLE_ARBITRATOR)

    def test_supplemental_before_decision_allowed(self):
        e = self.svc.add_supplemental(evt("sup-1", "supplemental_agreement", "2025-07-03T00:00:00Z",
                                          order_id="ORD-T-1", changes=[{"matter": "交付方式", "content": "分批"}]))
        self.assertEqual(e.seq, len(self.svc.journal))

    def test_supplemental_cannot_change_decided_matter(self):
        self._decide()
        with self.assertRaisesRegex(ImmutabilityError, "已裁定事项"):
            self.svc.add_supplemental(evt("sup-bad", "supplemental_agreement", "2025-07-03T00:00:00Z",
                                          order_id="ORD-T-1",
                                          changes=[{"matter": "合同是否成立", "content": "改为不成立"}]))

    def test_supplemental_cannot_override_used_evidence(self):
        self._decide()
        with self.assertRaisesRegex(ImmutabilityError, "已被裁定使用的证据"):
            self.svc.add_supplemental(evt("sup-ev", "supplemental_agreement", "2025-07-03T00:00:00Z",
                                          order_id="ORD-T-1",
                                          changes=[{"matter": "另一事项", "evidence_refs": ["sig-ORD-T-1"]}]))

    def test_supplemental_still_allowed_on_undecided_matter(self):
        self._decide()
        e = self.svc.add_supplemental(evt("sup-ok", "supplemental_agreement", "2025-07-03T00:00:00Z",
                                          order_id="ORD-T-1",
                                          changes=[{"matter": "交付方式", "content": "线上交付"}]))
        self.assertGreater(e.seq, 0)


class DecisionTest(unittest.TestCase):
    def setUp(self):
        self.svc = DisputeService(build_default_registry())
        seed(self.svc, order("ORD-D", when="2026-05-01T00:00:00Z"),
             auth(oid="ORD-D"), signature(oid="ORD-D"), payment(oid="ORD-D"), jurisdiction(oid="ORD-D"))

    def test_only_arbitrator_decides(self):
        for role in (ROLE_PARTY, ROLE_MEDIATOR, ROLE_OPERATOR, ROLE_COORDINATOR):
            with self.assertRaises(AuthorizationError):
                self.svc.make_decision(order_id="ORD-D", matter="m", ruling="r",
                                       rule_ids=["R1"], evidence_refs=[f"ord-ORD-D"],
                                       by="x", role=role)

    def test_decision_validates_refs_and_rules(self):
        with self.assertRaisesRegex(IntakeValidationError, "不存在的证据"):
            self.svc.make_decision(order_id="ORD-D", matter="m", ruling="r",
                                   rule_ids=["R1"], evidence_refs=["nope"],
                                   by="裁定人", role=ROLE_ARBITRATOR)
        with self.assertRaisesRegex(IntakeValidationError, "规则版本之外"):
            self.svc.make_decision(order_id="ORD-D", matter="m", ruling="r",
                                   rule_ids=["R99"], evidence_refs=[f"ord-ORD-D"],
                                   by="裁定人", role=ROLE_ARBITRATOR)
        with self.assertRaisesRegex(IntakeValidationError, "代理授权"):
            self.svc.make_decision(order_id="ORD-D", matter="m", ruling="r",
                                   rule_ids=["R1"], evidence_refs=[f"ord-ORD-D"],
                                   authorization_ref="auth-missing",
                                   by="裁定人", role=ROLE_ARBITRATOR)

    def test_decided_matter_and_evidence_lock(self):
        d = self.svc.make_decision(order_id="ORD-D", matter="合同是否成立", ruling="成立",
                                   rule_ids=["R1", "R2"], evidence_refs=[f"ord-ORD-D", f"sig-ORD-D"],
                                   by="裁定人", role=ROLE_ARBITRATOR)
        self.assertTrue(d.decision_id)
        with self.assertRaisesRegex(ImmutabilityError, "已有生效裁定"):
            self.svc.make_decision(order_id="ORD-D", matter="合同是否成立", ruling="再裁",
                                   rule_ids=["R1"], evidence_refs=[f"ord-ORD-D"],
                                   by="裁定人", role=ROLE_ARBITRATOR)
        self.assertEqual(self.svc.get_case("ORD-D").decided_matters["合同是否成立"], d.decision_id)

    def test_trace_links_agency_rules_and_evidence(self):
        self.svc.make_decision(order_id="ORD-D", matter="合同是否成立", ruling="成立",
                               rule_ids=["R1", "R2", "R3"],
                               evidence_refs=[f"ord-ORD-D", f"auth-ORD-D", f"sig-ORD-D", f"pay-ORD-D"],
                               authorization_ref=f"auth-ORD-D",
                               by="裁定人", role=ROLE_ARBITRATOR)
        trace = trace_decision(self.svc, "ORD-D", matter="合同是否成立")
        self.assertEqual(trace["agency_trigger"]["order_event_id"], "ord-ORD-D")
        self.assertEqual(trace["agent_authorization"]["authorization_event_id"], "auth-ORD-D")
        self.assertEqual([r["id"] for r in trace["rule_basis"]], ["R1", "R2", "R3"])
        self.assertTrue(all(r["version"] == "DTE-2026.1" for r in trace["rule_basis"]))
        chain_ids = [e["event_id"] for e in trace["evidence_chain"]]
        self.assertEqual(chain_ids, [f"ord-ORD-D", f"auth-ORD-D", f"sig-ORD-D", f"pay-ORD-D"])
        self.assertTrue(all(e["locked_by_decision"] for e in trace["evidence_chain"]))
        self.assertTrue(all(e["entry_hash"] for e in trace["journal_lead_up"]))
        self.assertEqual(trace["journal_lead_up"][0]["event_type"], "case_opened")


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self.svc = DisputeService(build_default_registry())
        seed(self.svc, order(), signature(), payment(), jurisdiction())
        self.svc.transition(order_id="ORD-T-1", to="mediation", by="协调员", role=ROLE_COORDINATOR)

    def test_status_transitions(self):
        self.assertEqual(self.svc.get_case("ORD-T-1").status, "mediation")
        svc = DisputeService(build_default_registry())
        seed(svc, order("ORD-W"), signature(oid="ORD-W"), payment(oid="ORD-W"), jurisdiction(oid="ORD-W"))
        svc.transition(order_id="ORD-W", to="withdrawn", by="协调员", role=ROLE_COORDINATOR)
        self.assertEqual(svc.get_case("ORD-W").status, "withdrawn")

    def test_deadlines_notices_fees_continue_after_mediation(self):
        self.svc.update_deadline(order_id="ORD-T-1", name="举证期限", at="2025-07-20T00:00:00Z",
                                 status="open", by="协调员", role=ROLE_COORDINATOR)
        self.svc.send_notice(order_id="ORD-T-1", template="session", to="卖方",
                             by="协调员", role=ROLE_COORDINATOR)
        self.svc.update_fee(order_id="ORD-T-1", name="调解费", amount=50, currency="USD",
                            status="payable", by="协调员", role=ROLE_COORDINATOR)
        self.svc.update_fee(order_id="ORD-T-1", name="调解费", amount=50, currency="USD",
                            status="paid", by="协调员", role=ROLE_COORDINATOR)
        case = self.svc.get_case("ORD-T-1")
        self.assertEqual(case.deadlines["举证期限"]["status"], "open")
        self.assertEqual(len(case.notices), 1)
        self.assertEqual(case.fees["调解费"]["status"], "paid")

    def test_lifecycle_roles(self):
        with self.assertRaises(AuthorizationError):
            self.svc.update_deadline(order_id="ORD-T-1", name="x", at="t", status="open",
                                     by="调解员", role=ROLE_MEDIATOR)
        with self.assertRaises(AuthorizationError):
            self.svc.transition(order_id="ORD-T-1", to="litigation", by="当事人", role=ROLE_PARTY)
        # 裁定人也可切换状态（如转诉讼）
        self.svc.transition(order_id="ORD-T-1", to="litigation", by="裁定人", role=ROLE_ARBITRATOR)
        self.assertEqual(self.svc.get_case("ORD-T-1").status, "litigation")


class JournalAndRecoveryTest(unittest.TestCase):
    def test_hash_chain_detects_tampering(self):
        j = Journal()
        j.append(timestamp="t1", event_type="order_event", payload={"a": 1}, event_id="e1", actor="x")
        j.append(timestamp="t2", event_type="signature_evidence", payload={"a": 2}, event_id="e2", actor="x")
        text = j.export_jsonl()
        lines = text.splitlines()
        bad = json.loads(lines[0])
        bad["payload"]["a"] = 999
        lines[0] = json.dumps(bad, ensure_ascii=False, sort_keys=True)
        with self.assertRaises(ChainIntegrityError):
            Journal.from_jsonl("\n".join(lines))

    def test_hash_chain_detects_reorder(self):
        j = Journal()
        j.append(timestamp="t1", event_type="a", payload={}, event_id="e1", actor="x")
        j.append(timestamp="t2", event_type="b", payload={}, event_id="e2", actor="x")
        lines = j.export_jsonl().splitlines()
        with self.assertRaises(ChainIntegrityError):
            Journal.from_jsonl("\n".join(reversed(lines)))

    def test_recover_rebuilds_state_and_package(self):
        svc = DisputeService(build_default_registry())
        seed(svc, order(), auth(), signature(), consent(), payment(), jurisdiction())
        svc.access_original(order_id="ORD-T-1", docs=["sig-ORD-T-1"], actor="调解员",
                            role=ROLE_MEDIATOR, reason="核验")
        svc.make_decision(order_id="ORD-T-1", matter="合同是否成立", ruling="成立",
                          rule_ids=["R1", "R2"], evidence_refs=["ord-ORD-T-1", "sig-ORD-T-1"],
                          by="裁定人", role=ROLE_ARBITRATOR)
        pkg = build_package(svc, "ORD-T-1")
        log = svc.export_log()

        recovered = DisputeService.recover(log, build_default_registry())
        case = recovered.get_case("ORD-T-1")
        self.assertEqual(case.rule_version, "DTE-2025.2")
        self.assertEqual(case.route, "mediation")
        self.assertEqual(len(case.audit), 1)
        self.assertIn("合同是否成立", case.decided_matters)
        pkg2 = build_package(recovered, "ORD-T-1")
        self.assertEqual(pkg["package_digest"], pkg2["package_digest"])
        t1 = trace_decision(svc, "ORD-T-1", matter="合同是否成立")
        t2 = trace_decision(recovered, "ORD-T-1", matter="合同是否成立")
        self.assertEqual(t1["trace_digest"], t2["trace_digest"])

    def test_recover_rejects_corrupt_log(self):
        svc = DisputeService(build_default_registry())
        seed(svc, order())
        log = svc.export_log()
        lines = log.splitlines()
        bad = json.loads(lines[-1])
        bad["payload"]["amount"] = 1
        lines[-1] = json.dumps(bad, ensure_ascii=False, sort_keys=True)
        with self.assertRaises(ChainIntegrityError):
            DisputeService.recover("\n".join(lines), build_default_registry())


class PackageTest(unittest.TestCase):
    def test_package_contents_and_digest(self):
        svc = DisputeService(build_default_registry())
        seed(svc, order(), signature(), payment(), jurisdiction())
        svc.add_operator_fact(fact(), actor="平台", role=ROLE_OPERATOR)
        pkg = build_package(svc, "ORD-T-1")
        self.assertEqual(pkg["package_format"], "dispute-package/1")
        self.assertEqual(pkg["rule_version"]["version"], "DTE-2025.2")
        self.assertTrue(any(r.startswith("R1 ") for r in pkg["rule_version"]["rules"]))
        self.assertTrue(pkg["chain"]["anchors"][0]["prev_hash"] == "0" * 64)
        self.assertEqual(pkg["chain"]["journal_head"], svc.journal.head)
        self.assertEqual(len(pkg["operator_facts"]), 1)
        # 摘要稳定
        self.assertEqual(pkg["package_digest"], build_package(svc, "ORD-T-1")["package_digest"])

    def test_demo_fixture_runs(self):
        from src.dispute_archive.demo import run_demo

        svc, summary = run_demo(FIXTURE)
        self.assertTrue(summary["case_a"]["duplicate_event_returns_same_case"])
        self.assertEqual(summary["case_a"]["rule_version"], "DTE-2025.2")
        self.assertTrue(summary["case_b"]["frozen"])
        self.assertTrue(summary["recovery"]["package_digest_match"])
        self.assertTrue(summary["recovery"]["trace_evidences_match"])
        self.assertEqual(summary["case_a"]["fee_status"], "paid")


if __name__ == "__main__":
    unittest.main()
