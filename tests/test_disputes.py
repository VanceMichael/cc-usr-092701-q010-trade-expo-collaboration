import json
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from src.trade_expo_collaboration.disputes import (
    DuplicateEvent,
    DisputeCaseLog,
    DisputeService,
    PendingOrder,
    RuleSet,
    event_fingerprint,
    validate_event,
)

FIXTURE = Path("fixtures/dispute_case.json")
BUYER = "杭州云舶跨境电商有限公司"
SELLER = "Lumina Digital Pte. Ltd."
AGENT = "云舶采购智能助手 v3"

ORDER = {
    "type": "order",
    "source": "数贸会交易平台订单服务",
    "order_ref": "PO-20260910-7781",
    "ordered_at": "2026-09-10",
    "amount": 3280.0,
    "currency": "CNY",
    "actor_party": BUYER,
    "placed_by_agent": AGENT,
}
AGENCY = {
    "type": "agency",
    "source": "采购方代理管理后台",
    "order_ref": "PO-20260910-7781",
    "principal": BUYER,
    "agent": AGENT,
    "scope": ["自动下单", "订单确认"],
    "valid_from": "2026-01-01",
    "valid_to": "2026-12-31",
}
SIGNATURE = {
    "type": "signature",
    "source": "签鉴通电子签名平台",
    "order_ref": "PO-20260910-7781",
    "signed_doc_ref": "CTR-PO-20260910-7781",
    "signer_party": BUYER,
    "signed_at": "2026-09-10",
    "digest": "9f2c41a87be6d3f0a51c7e94b2d80f613a27c9e054d1b8f3a6c0e2749d1b8f30",
    "contract_amount": 3280.0,
    "matches_order": True,
}
CONSENT = {
    "type": "consent",
    "source": "采购方隐私授权中心",
    "order_ref": "PO-20260910-7781",
    "granting_party": BUYER,
    "data_categories": ["交易信息", "支付信息", "合同信息", "身份信息"],
    "destinations": ["新加坡国际商事调解中心", "星桥支付（新加坡）"],
    "granted_at": "2026-09-10",
    "valid_from": "2026-09-10",
    "valid_to": "2027-09-09",
    "allow_mediator_view": True,
}
PAYMENT = {
    "type": "payment",
    "source": "星桥支付（新加坡）跨境结算机构",
    "order_ref": "PO-20260910-7781",
    "payment_ref": "PAY-XB-559012",
    "payer_party": BUYER,
    "payee_party": SELLER,
    "amount": 3280.0,
    "currency": "CNY",
    "recorded_at": "2026-09-11",
}
JURISDICTION = {
    "type": "jurisdiction",
    "source": "数贸会交易平台合同模板库",
    "order_ref": "PO-20260910-7781",
    "clause_ref": "JC-2026-PLATFORM-07",
    "forum": "新加坡国际商事调解中心先行调解",
    "governing_law": "中华人民共和国法律",
    "agreed_at": "2026-09-10",
}


def clock_at(day: str):
    return lambda: datetime.fromisoformat(day).replace(tzinfo=timezone.utc)


def build_full_case(events=None, rule=None):
    """按序摄取一整套材料，返回 (service, case)。"""
    service = DisputeService(rule=rule or RuleSet.v1(), clock=clock_at("2026-09-12"))
    case = None
    for ev in events or [ORDER, AGENCY, SIGNATURE, CONSENT, PAYMENT, JURISDICTION]:
        case, action = service.ingest(deepcopy(ev))
    return service, case


class IngestAndTriageTest(unittest.TestCase):
    def test_fixture_loads_into_small_claim_fast_track(self):
        data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        service = DisputeService(clock=clock_at("2026-09-17"))
        case = None
        actions = []
        for ev in data["events"]:
            case, action = service.ingest(ev)
            actions.append(action)
        self.assertEqual(actions[0], "created")
        self.assertIn("received", actions)
        self.assertEqual(case.status, "triage")
        self.assertFalse(case.frozen)
        self.assertEqual(case.triage(), "小额速调通道")
        self.assertEqual(len(case.events), 8)
        # 争议包按案件发生时有效的规则版本生成
        package = service.build_package(case.case_id)
        self.assertEqual(package["rule_snapshot"]["version"], data["rule_version"])
        self.assertEqual(package["summary"]["frozen"], False)
        self.assertEqual(len(package["integrity"]["event_fingerprints"]), 8)

    def test_duplicate_event_returns_original_case(self):
        service, case = build_full_case()
        again, action = service.ingest(deepcopy(PAYMENT))
        self.assertEqual(action, "duplicated")
        self.assertIs(again, case)
        self.assertEqual(len(service.cases), 1)
        # 支付流水重复提交不得产生第二份原件
        payments = [e for e in case.events if e["type"] == "payment"]
        self.assertEqual(len(payments), 1)

    def test_duplicate_order_raises(self):
        service, _ = build_full_case()
        with self.assertRaises(DuplicateEvent):
            service.ingest(deepcopy(ORDER))

    def test_early_agency_buffered_until_order_arrives(self):
        service = DisputeService(clock=clock_at("2026-09-12"))
        with self.assertRaises(PendingOrder):
            service.ingest(deepcopy(AGENCY))
        # 暂存阶段重复提交同一授权识别为重复事件，不重复暂存
        with self.assertRaises(DuplicateEvent):
            service.ingest(deepcopy(AGENCY))
        case, action = service.ingest(deepcopy(ORDER))
        self.assertEqual(action, "created")
        agency_events = [e for e in case.events if e["type"] == "agency"]
        self.assertEqual(len(agency_events), 1)  # 缓冲队列内重复未造成两件
        # 补登记后其余材料正常摄取
        service.ingest(deepcopy(SIGNATURE))
        service.ingest(deepcopy(CONSENT))
        service.ingest(deepcopy(PAYMENT))
        service.ingest(deepcopy(JURISDICTION))
        self.assertEqual(case._agency_ref_for_order(), agency_events[0]["event_ref"])

    def test_validation_requires_source_and_business_fields(self):
        bad = {"type": "order", "order_ref": "X", "ordered_at": "2026-09-10",
               "amount": 1, "currency": "CNY", "actor_party": BUYER}
        with self.assertRaisesRegex(ValueError, "source"):
            validate_event(bad)
        with self.assertRaisesRegex(ValueError, "未知事件类型"):
            validate_event({"type": "nope", "source": "x"})
        with self.assertRaisesRegex(ValueError, "金额必须为正数"):
            validate_event({**ORDER, "amount": 0})


class ConflictFreezeTest(unittest.TestCase):
    def test_signature_payment_amount_mismatch_freezes_triage(self):
        broken_payment = {**PAYMENT, "amount": 2980.0}
        service, case = build_full_case(
            [ORDER, AGENCY, SIGNATURE, CONSENT, broken_payment, JURISDICTION]
        )
        self.assertTrue(case.frozen)
        self.assertEqual(case.triage(), "人工核证队列")
        self.assertTrue(any("金额" in r for r in case.freeze_reasons))

    def test_payment_before_signature_without_preauthorization_freezes(self):
        early_payment = {**PAYMENT, "recorded_at": "2026-09-08"}
        service, case = build_full_case(
            [ORDER, AGENCY, SIGNATURE, CONSENT, early_payment, JURISDICTION]
        )
        self.assertTrue(any("早于签署" in r for r in case.freeze_reasons))
        self.assertEqual(case.triage(), "人工核证队列")

    def test_multi_settlement_payment_sums_before_compare(self):
        split = {**PAYMENT, "amount": 3280.0, "settlements": [
            {"settler": "星桥支付", "amount": 3000.0},
            {"settler": "备付金账户", "amount": 280.0},
        ]}
        service, case = build_full_case(
            [ORDER, AGENCY, SIGNATURE, CONSENT, split, JURISDICTION]
        )
        self.assertFalse(case.frozen)

    def test_missing_evidence_routes_to_correction_queue(self):
        service, case = build_full_case([ORDER, AGENCY, SIGNATURE])
        self.assertFalse(case.frozen)
        self.assertEqual(case.triage(), "补正通知队列")


class AccessControlTest(unittest.TestCase):
    def test_mediator_sees_only_authorized_originals(self):
        service, case = build_full_case()
        seen = case.request_view(actor="调解员甲", role="mediator",
                                 purpose="庭前阅卷，核验合同是否成立")
        refs = {e["event_ref"] for e in seen}
        # 采购方同意覆盖的原件（订单/签名/支付/授权）均可阅
        self.assertIn(case.order_event["event_ref"], refs)
        log = case.access_log[-1]
        self.assertTrue(log.granted)
        self.assertEqual(log.action, "view")
        self.assertIn("核验合同", log.purpose)

    def test_mediator_denied_without_authorization_is_logged(self):
        no_view_consent = {**CONSENT, "allow_mediator_view": False}
        service, case = build_full_case(
            [ORDER, AGENCY, SIGNATURE, no_view_consent, PAYMENT, JURISDICTION]
        )
        with self.assertRaisesRegex(PermissionError, "获授权"):
            case.request_view(actor="调解员甲", role="mediator",
                              event_refs=[case.order_event["event_ref"]],
                              purpose="庭前阅卷")
        self.assertFalse(case.access_log[-1].granted)
        self.assertIn("未获该原件授权", case.access_log[-1].reason_if_denied)

    def test_party_sees_only_own_originals(self):
        service, case = build_full_case()
        with self.assertRaises(PermissionError):
            case.request_view(actor=SELLER, role="party",
                              event_refs=[case.order_event["event_ref"]],
                              purpose="查看对方订单原件")
        own = case.request_view(actor=SELLER, role="party", purpose="本方材料自查")
        # 卖方不持有以其为所有者的订单/支付原件；管辖约定为共同材料，对当事人可见
        self.assertTrue(own)
        self.assertTrue(all(e["type"] == "jurisdiction" for e in own))

    def test_operator_may_add_fact_but_not_statement(self):
        service, case = build_full_case()
        fact = {
            "type": "fact", "source": "平台风控台账", "order_ref": "PO-20260910-7781",
            "fact_ref": "FACT-01", "content": "下单会话日志显示智能助手触发了预算阈值提醒",
            "recorded_at": "2026-09-13",
        }
        stored, _ = case.submit_fact(role="operator", actor="平台运营值班员", event=fact)
        self.assertEqual(stored["type"], "fact")
        forged = {
            "type": "statement", "source": "后台代录", "order_ref": "PO-20260910-7781",
            "speaker_party": BUYER, "matter": "合同成立",
            "content": "（运营者试图代当事人改写陈述）", "stated_at": "2026-09-13",
        }
        with self.assertRaisesRegex(PermissionError, "无权改写"):
            case.submit_fact(role="operator", actor="平台运营值班员", event=forged)
        with self.assertRaisesRegex(PermissionError, "仅可补充交易事实"):
            case.submit_fact(role="operator", actor="平台运营值班员", event=deepcopy(PAYMENT))

    def test_party_cannot_rewrite_prior_statement(self):
        service, case = build_full_case()
        first = {
            "type": "statement", "source": "采购方表单", "order_ref": "PO-20260910-7781",
            "matter": "合同成立", "content": "最初陈述：未二次确认", "stated_at": "2026-09-15",
        }
        case.submit_fact(role="party", actor=BUYER, event=first)
        rewrite = {**first, "content": "替换后的陈述：完全认可"}
        with self.assertRaisesRegex(PermissionError, "不可被改写"):
            case.submit_fact(role="party", actor=BUYER, event=rewrite)
        # 追加新陈述（不同内容、不同时间）允许，但指纹含时间因此算新事件
        addendum = {**first, "stated_at": "2026-09-18", "content": "补充说明：预算提醒确有弹出"}
        stored, _ = case.submit_fact(role="party", actor=BUYER, event=addendum)
        self.assertTrue(stored["immutable"])


class CrossBorderTransferTest(unittest.TestCase):
    def test_transfer_allowed_to_consented_destination_and_logged(self):
        service, case = build_full_case()
        pay_ref = next(e["event_ref"] for e in case.events if e["type"] == "payment")
        case.transfer(actor="案件协调员", role="operator",
                      destination="新加坡国际商事调解中心",
                      event_refs=[pay_ref], purpose="应调解员要求移送支付流水核验")
        record = case.access_log[-1]
        self.assertEqual(record.action, "transfer")
        self.assertTrue(record.granted)
        self.assertEqual(record.destination, "新加坡国际商事调解中心")

    def test_transfer_blocked_without_export_consent(self):
        service, case = build_full_case()
        pay_ref = next(e["event_ref"] for e in case.events if e["type"] == "payment")
        with self.assertRaisesRegex(PermissionError, "出境同意"):
            case.transfer(actor="案件协调员", role="operator",
                          destination="某未授权第三国仲裁机构",
                          event_refs=[pay_ref], purpose="试探性移送")
        record = case.access_log[-1]
        self.assertFalse(record.granted)
        self.assertIn("出境同意", record.reason_if_denied)
        # 被拒绝的转交不得改变原件或分流
        self.assertTrue(all(e["type"] != "transfer" for e in case.events))


class LifecycleAndRulingTest(unittest.TestCase):
    def _mediation_case(self):
        service, case = build_full_case()
        case.enter_mediation(on="2026-09-20")
        return service, case

    def test_enter_mediation_sets_deadline_notice_and_fee(self):
        _, case = self._mediation_case()
        self.assertEqual(case.status, "mediation")
        self.assertEqual(case.deadlines["response"], "2026-09-30")
        self.assertTrue(any("调解受理通知" in n["text"] for n in case.notices))
        names = [f["name"] for f in case.fees["records"]]
        self.assertIn("调解服务费", names)

    def test_advance_deadlines_and_fee_status(self):
        _, case = self._mediation_case()
        result = case.advance("2026-10-01")
        self.assertTrue(any("response 期限" in t for t in result["triggered"]))
        for fee in case.fees["records"]:
            self.assertEqual(fee["status"], "payable")
        case.record_payment_of_fee("案件受理费", paid_on="2026-10-02")
        intake = next(f for f in case.fees["records"] if f["name"] == "案件受理费")
        self.assertEqual(intake["status"], "paid")
        self.assertEqual(intake["paid_on"], "2026-10-02")

    def test_ruling_and_trace_back_to_agency_rule_and_evidence(self):
        _, case = self._mediation_case()
        ruling = case.issue_ruling(
            matters=["合同成立"],
            outcome="合同成立：智能助手在有效授权范围内下单，签署与支付相互印证。",
            issued_on="2026-09-25",
        )
        self.assertEqual(ruling.rule_version, "2026.09")
        self.assertEqual(case.ruled_matters["合同成立"], ruling.ruling_id)
        trace = case.trace_ruling(ruling.ruling_id)
        # 可追溯触发裁定的代理行为
        self.assertEqual(trace["agency_action"]["type"], "agency")
        self.assertEqual(trace["agency_action"]["agent"], AGENT)
        # 规则依据为案件发生时版本
        self.assertEqual(trace["rule_basis"]["version"], "2026.09")
        # 完整证据链覆盖六类核心材料
        chain_types = {e["type"] for e in trace["evidence_chain"]}
        self.assertEqual(
            chain_types, {"order", "agency", "signature", "consent", "payment", "jurisdiction"}
        )
        # 签发时的核验审计可回溯
        self.assertIsNotNone(trace["audit_at_issue"])
        self.assertEqual(trace["audit_at_issue"]["seq"], ruling.audit_seq)

    def test_ruling_requires_known_matter(self):
        _, case = self._mediation_case()
        with self.assertRaisesRegex(ValueError, "没有事实/协议基础"):
            case.issue_ruling(matters=["不存在的事项"], outcome="x")

    def test_supplement_only_changes_unruled_matters(self):
        _, case = self._mediation_case()
        ruling = case.issue_ruling(
            matters=["合同成立"], outcome="合同成立", issued_on="2026-09-25"
        )
        pay_ref = next(e["event_ref"] for e in case.events if e["type"] == "payment")
        # 未裁定事项 + 不动用已使用证据：允许
        ok = {
            "type": "supplement", "source": "双方签署端口",
            "order_ref": "PO-20260910-7781",
            "parties": [BUYER, SELLER], "matters": ["交付时间"],
            "effective_at": "2026-09-26",
            "changes": {"交付时间": {"to": "2026-10-15"}},
        }
        stored, _ = case.receive(ok)
        self.assertEqual(stored["accepted_matters"], ["交付时间"])
        # 已裁定事项：拒绝
        ruled = {**ok, "matters": ["合同成立"], "effective_at": "2026-09-27"}
        with self.assertRaisesRegex(ValueError, "已裁定事项"):
            case.receive(ruled)
        # 试图覆盖已在裁定中使用的证据：拒绝
        overwrite = {
            **ok, "matters": ["支付安排"], "effective_at": "2026-09-28",
            "changes": {"支付安排": {"replaces_event_ref": pay_ref}},
        }
        with self.assertRaisesRegex(ValueError, "已在裁定中使用的证据"):
            case.receive(overwrite)
        # 原支付流水未被改动
        pay = next(e for e in case.events if e["event_ref"] == pay_ref)
        self.assertEqual(pay["amount"], PAYMENT["amount"])

    def test_withdraw_keeps_deadlines_notices_fees_moving(self):
        service = DisputeService(clock=clock_at("2026-09-12"))
        case = None
        for ev in [ORDER, AGENCY, SIGNATURE, CONSENT, PAYMENT, JURISDICTION]:
            case, _ = service.ingest(deepcopy(ev))
        case.withdraw(on="2026-09-21")
        self.assertEqual(case.status, "withdrawn")
        self.assertEqual(case.deadlines["archive"], "2027-03-20")
        self.assertTrue(any("撤回确认通知" in n["text"] for n in case.notices))
        self.assertTrue(any("减半" in f["name"] for f in case.fees["records"]))
        # 撤回后期限推进仍然工作
        result = case.advance("2027-03-21")
        self.assertTrue(any("archive 期限" in t for t in result["triggered"]))

    def test_enter_litigation_sets_answer_deadline_notice_and_fee(self):
        _, case = build_full_case()
        case.enter_litigation(on="2026-09-22")
        self.assertEqual(case.status, "litigation")
        self.assertEqual(case.deadlines["answer"], "2026-10-07")
        self.assertTrue(any("诉讼应诉通知" in n["text"] for n in case.notices))
        self.assertTrue(any(f["name"] == "立案费" for f in case.fees["records"]))
        # 诉讼中仍可出具裁定并溯源
        ruling = case.issue_ruling(matters=["合同成立"], outcome="合同成立",
                                   issued_on="2026-10-01")
        trace = case.trace_ruling(ruling.ruling_id)
        self.assertEqual(trace["ruling"]["rule_version"], "2026.09")
        self.assertEqual(case.status, "resolved")

    def test_frozen_case_can_still_enter_mediation_with_notice(self):
        broken = {**PAYMENT, "amount": 1.0}
        service, case = build_full_case(
            [ORDER, AGENCY, SIGNATURE, CONSENT, broken, JURISDICTION]
        )
        case.enter_mediation(on="2026-09-20")
        self.assertEqual(case.status, "mediation")
        self.assertTrue(any("自动分流冻结中" in n["text"] for n in case.notices))


class SnapshotRecoveryTest(unittest.TestCase):
    def test_snapshot_restore_continues_state(self):
        _, case = build_full_case()
        case.enter_mediation(on="2026-09-20")
        ruling = case.issue_ruling(matters=["合同成立"], outcome="合同成立",
                                   issued_on="2026-09-25")
        snap = case.to_snapshot()

        restored = DisputeCaseLog.from_snapshot(snap, RuleSet.v1(), clock=clock_at("2026-09-26"))
        self.assertEqual(restored.status, case.status)
        self.assertEqual(restored.case_id, case.case_id)
        self.assertEqual(len(restored.events), len(case.events))
        self.assertEqual(restored.ruled_matters, {"合同成立": ruling.ruling_id})
        # 恢复后继续推进期限与费用（裁定日 2026-09-25 + 180 天归档期限届满后）
        result = restored.advance("2027-04-01")
        self.assertTrue(any("archive 期限" in t for t in result["triggered"]))
        # 恢复后重复提交仍返回同一原件
        stored, ignored = restored.receive(deepcopy(PAYMENT))
        self.assertTrue(ignored)

    def test_restore_rejects_wrong_rule_version(self):
        _, case = build_full_case()
        snap = case.to_snapshot()
        other = RuleSet(version="2027.01", effective_date=__import__("datetime").date(2027, 1, 1),
                        small_claim_threshold=1.0, data_export_requires_consent=False)
        with self.assertRaisesRegex(ValueError, "案件发生时有效"):
            DisputeCaseLog.from_snapshot(snap, other)

    def test_fingerprint_stable(self):
        self.assertEqual(event_fingerprint(PAYMENT), event_fingerprint(deepcopy(PAYMENT)))
        self.assertNotEqual(event_fingerprint(PAYMENT),
                            event_fingerprint({**PAYMENT, "payment_ref": "OTHER"}))


if __name__ == "__main__":
    unittest.main()
