"""跨境数字订单争议归档与分流服务。

围绕一份在提交时被快照的规则版本，把订单事件、代理授权、签名证据、
数据处理同意、支付流水、管辖约定与当事人陈述归档为可复核的争议包：

- 相同事件重复提交按事件指纹返回原案件；
- 签名证据与支付流水互相矛盾时冻结自动分流，转人工队列；
- 调解员仅能查看获授权的原件，平台运营者可补充事实但不得改写陈述；
- 每次数据访问与跨境转交都记录原因并落入争议包；
- 后续补充协议只改变尚未裁定的事项，不覆盖已使用证据；
- 调解、诉讼、撤回后仍推进期限、通知与费用状态；
- 从一项裁定可追溯代理行为、规则依据与完整证据链，并支持快照恢复。

数据均为演示用虚构内容；模块刻意保持无第三方依赖。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Optional

EventType = Literal[
    "order",          # 订单事件（可由智能助手自动下单）
    "agency",         # 代理授权（principal -> agent，含范围与有效期）
    "signature",      # 电子签名证据
    "consent",        # 数据处理/出境同意
    "payment",        # 支付流水（可能由另一家结算机构出具）
    "jurisdiction",   # 管辖约定
    "statement",      # 当事人陈述
    "supplement",     # 补充协议
    "fact",           # 平台运营者补充的交易事实
]

Role = Literal["mediator", "operator", "party"]

VALID_EVENT_TYPES: frozenset[str] = frozenset(
    t for t in EventType.__args__  # type: ignore[attr-defined]
)

# 每种事件登记时必须带有的业务字段
REQUIRED_FIELDS: dict[str, set[str]] = {
    "order": {"order_ref", "ordered_at", "amount", "currency", "actor_party"},
    "agency": {"principal", "agent", "scope", "valid_from", "valid_to"},
    "signature": {"signed_doc_ref", "signer_party", "signed_at", "digest"},
    "consent": {"granting_party", "data_categories", "destinations", "granted_at"},
    "payment": {"payment_ref", "payer_party", "payee_party", "amount", "currency", "recorded_at"},
    "jurisdiction": {"clause_ref", "forum", "governing_law", "agreed_at"},
    "statement": {"speaker_party", "content", "stated_at"},
    "supplement": {"parties", "matters", "effective_at"},
    "fact": {"fact_ref", "content", "recorded_at"},
}

# 自动分流前需要收齐的材料（按案件发生时有效的规则版本判定）
CORE_EVIDENCE: tuple[str, ...] = (
    "order", "agency", "signature", "consent", "payment", "jurisdiction",
)

# 生命周期
INTAKE = "intake"          # 收件
TRIAGE = "triage"          # 自动分流（可能被冻结）
MEDIATION = "mediation"    # 调解中
LITIGATION = "litigation"  # 诉讼中
WITHDRAWN = "withdrawn"    # 已撤回
RESOLVED = "resolved"      # 已就全部事项出具裁定并结案

# 通知类型 -> 基准期限（自然日）。期限起算以状态触发日为准。
DEADLINES: dict[str, int] = {
    "answer": 15,        # 应诉/答复期限
    "response": 10,      # 响应期限
    "appeal": 30,        # 上诉/异议期限
    "archive": 180,      # 结案/撤回后归档保存提示
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _is_blank(text: object) -> bool:
    return not isinstance(text, str) or not text.strip()


def event_fingerprint(event: dict[str, Any]) -> str:
    """同一业务事件的稳定指纹：类型 + 业务主键。"""
    etype = event.get("type")
    key_map = {
        "order": ("order_ref",),
        "payment": ("payment_ref",),
        "signature": ("signed_doc_ref", "signer_party"),
        "agency": ("principal", "agent", "valid_from"),
        "consent": ("granting_party", "data_categories", "granted_at"),
        "jurisdiction": ("clause_ref",),
        "statement": ("speaker_party", "stated_at", "content"),
        "supplement": ("parties", "matters", "effective_at"),
        "fact": ("fact_ref",),
    }
    basis: dict[str, Any] = {"type": etype}
    for key in key_map.get(etype, ()):
        basis[key] = event.get(key)
    raw = json.dumps(basis, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def validate_event(event: object) -> dict[str, Any]:
    """校验单条输入事件的结构。"""
    if not isinstance(event, dict):
        raise ValueError("事件必须是对象")
    etype = event.get("type")
    if etype not in VALID_EVENT_TYPES:
        raise ValueError(f"未知事件类型：{etype!r}")
    missing = {f for f in REQUIRED_FIELDS[etype] if event.get(f) in (None, "")}
    if missing:
        raise ValueError(f"{etype} 事件缺少字段：" + ",".join(sorted(missing)))
    if _is_blank(event.get("source")):
        raise ValueError(f"{etype} 事件必须注明来源 source")
    if etype == "order":
        if not isinstance(event["amount"], (int, float)) or event["amount"] <= 0:
            raise ValueError("订单金额必须为正数")
    if etype in ("consent", "supplement"):
        list_fields = ("data_categories", "destinations") if etype == "consent" else ("parties", "matters")
        for name in list_fields:
            value = event[name]
            if not isinstance(value, list) or not value or any(_is_blank(x) for x in value):
                raise ValueError(f"{name} 必须是非空字符串数组")
    if etype == "supplement":
        changes = event.get("changes", {})
        if not isinstance(changes, dict) or not changes:
            raise ValueError("supplement 必须携带非空 changes 对象")
    return event


@dataclass(frozen=True)
class RuleSet:
    """案件发生时有效的规则版本，提交后只读。"""

    version: str
    effective_date: date
    small_claim_threshold: float          # 小额争议门槛
    data_export_requires_consent: bool    # 数据出境是否需要单独同意
    required_evidence: tuple[str, ...] = CORE_EVIDENCE
    routing: dict[str, str] = field(default_factory=dict)  # 去向 -> 队列名

    @classmethod
    def v1(cls) -> "RuleSet":
        return cls(
            version="2026.09",
            effective_date=date(2026, 9, 1),
            small_claim_threshold=5000.0,
            data_export_requires_consent=True,
            routing={
                "manual_review": "人工核证队列",
                "missing_evidence": "补正通知队列",
                "small_claim_fast_track": "小额速调通道",
                "general_mediation": "普通调解通道",
            },
        )


@dataclass
class AccessRecord:
    """一次数据访问或跨境转交的审计记录。"""

    seq: int
    ts: str
    actor: str
    role: str
    action: Literal["view", "transfer"]
    event_refs: list[str]
    purpose: str
    destination: Optional[str] = None
    granted: bool = True
    reason_if_denied: Optional[str] = None
    rule_version: str = ""


@dataclass
class Ruling:
    """一项裁定及其证据链指针。"""

    ruling_id: str
    issued_on: str
    matters: list[str]
    outcome: str
    agency_event_ref: Optional[str]   # 触发裁定的代理行为
    rule_version: str                # 所依据的规则版本
    evidence_refs: list[str]         # 完整证据链
    audit_seq: int                   # 签发时的访问审计序号


class DuplicateEvent(Exception):
    """相同业务事件重复提交（服务层据此返回原案件）。"""


class PendingOrder(Exception):
    """非订单事件早于订单到达，已按订单号暂存。"""


class DisputeCaseLog:
    """单案的写侧：收件、冻结分流、补充协议、访问控制、生命周期与溯源。"""

    def __init__(self, rule: RuleSet, clock: Callable[[], datetime] = _utcnow):
        self.rule = rule
        self.clock = clock
        self.case_id = ""
        self.order_event: Optional[dict[str, Any]] = None
        self.events: list[dict[str, Any]] = []
        self._by_fingerprint: dict[str, dict[str, Any]] = {}
        self.freeze_reasons: list[str] = []
        self.status = INTAKE
        self.route_queue: Optional[str] = None
        self.deadlines: dict[str, str] = {}
        self.notices: list[dict[str, Any]] = []
        self.fees: dict[str, Any] = {"currency": "CNY", "records": []}
        self.rulings: list[Ruling] = []
        self.ruled_matters: dict[str, str] = {}  # 事项 -> 生效裁定编号
        self.access_log: list[AccessRecord] = []
        self._access_seq = 0
        self._notice_seq = 0
        self._event_seq = 0

    # ---------- 收件 ----------

    def open_case(self, event: dict[str, Any]) -> dict[str, Any]:
        """以订单事件立案。"""
        validate_event(event)
        if event["type"] != "order":
            raise ValueError("必须以订单事件立案")
        if self.order_event is not None:
            raise DuplicateEvent(event_fingerprint(event))
        stored = self._store(event)
        self.order_event = stored
        self.case_id = "DSP-" + hashlib.sha256(
            (event["order_ref"] + "|" + event["ordered_at"]).encode("utf-8")
        ).hexdigest()[:12]
        self.fees["records"].append(
            {"name": "案件受理费", "status": "due", "amount": self._fee_for(event),
             "raised_on": self._today()}
        )
        self.status = TRIAGE
        self._reevaluate_route()
        return stored

    def receive(self, event: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """登记一条非订单事件。返回 (事件, 是否因重复被忽略)。"""
        validate_event(event)
        fp = event_fingerprint(event)
        if fp in self._by_fingerprint:
            return self._by_fingerprint[fp], True
        if event["type"] == "supplement":
            stored = self._apply_supplement(event)
            if self.status == RESOLVED:
                # 补充协议引入未裁定的新事项：案件恢复审理，不动既往裁定
                self.status = MEDIATION
                self.deadlines.pop("archive", None)
                self._notify("补充协议新增未裁定事项，案件恢复审理", None)
        else:
            stored = self._store(event)
        if event["type"] == "statement":
            self._assert_statement_not_rewrite(stored)
        self._detect_conflicts(stored)
        self._reevaluate_route()
        return stored, False

    # ---------- 自动分流 ----------

    def triage(self) -> str:
        """计算并记录自动分流去向；存在矛盾时冻结为人工核证队列。"""
        for ev in list(self._by_fingerprint.values()):
            self._detect_conflicts(ev)
        self._reevaluate_route()
        return self.route_queue or "待补材料"

    @property
    def frozen(self) -> bool:
        return bool(self.freeze_reasons)

    def _reevaluate_route(self) -> None:
        if self.freeze_reasons:
            self.route_queue = self.rule.routing["manual_review"]
            return
        have = {e["type"] for e in self.events}
        missing = [t for t in self.rule.required_evidence if t not in have]
        if missing:
            self.route_queue = self.rule.routing["missing_evidence"]
        elif self.order_event and self.order_event["amount"] <= self.rule.small_claim_threshold:
            self.route_queue = self.rule.routing["small_claim_fast_track"]
        else:
            self.route_queue = self.rule.routing["general_mediation"]

    def _store(self, event: dict[str, Any]) -> dict[str, Any]:
        self._event_seq += 1
        fp = event_fingerprint(event)
        ref = f"EV-{self._event_seq:04d}"
        stored = dict(event)
        stored["event_ref"] = ref
        stored["fingerprint"] = fp
        stored["received_at"] = self.clock().isoformat()
        stored["immutable"] = True  # 原件不可变；变更只能走补充协议或新事件
        self.events.append(stored)
        self._by_fingerprint[fp] = stored
        return stored

    # ---------- 矛盾检测：冻结自动分流 ----------

    def _detect_conflicts(self, new: dict[str, Any]) -> None:
        if new["type"] == "signature":
            self._check_signature_payment(new)
            self._check_signature_order(new)
            self._check_agency_scope(new)
        elif new["type"] == "payment":
            for ev in self.events:
                if ev["type"] == "signature":
                    self._check_signature_payment(ev, new)
        elif new["type"] == "agency":
            self._check_agency_conflict(new)

    def _check_signature_payment(self, sig: dict[str, Any],
                                 payment: Optional[dict[str, Any]] = None) -> None:
        payments = [payment] if payment else [e for e in self.events if e["type"] == "payment"]
        for pay in payments:
            if pay is None:
                continue
            pay_total = self._payment_total(pay)
            sig_total = sig.get("contract_amount")
            if sig_total is not None and not _amounts_equal(pay_total, sig_total):
                self._freeze(
                    f"签名证据 {sig['event_ref']} 载明合同金额 {sig_total}"
                    f" 与支付流水 {pay['event_ref']} 金额 {pay_total} 不一致"
                )
            signed_at = sig.get("signed_at")
            recorded_at = pay.get("recorded_at")
            if signed_at and recorded_at and recorded_at < signed_at and not sig.get("preauthorization_ref"):
                self._freeze(
                    f"支付流水 {pay['event_ref']} 早于签署 {sig['event_ref']} 且无预授权说明"
                )

    def _check_signature_order(self, sig: dict[str, Any]) -> None:
        order = self.order_event
        if not order or sig.get("matches_order", True):
            return
        self._freeze(f"签名证据 {sig['event_ref']} 对应单据与订单 {order['order_ref']} 不匹配")

    def _check_agency_scope(self, sig: dict[str, Any]) -> None:
        # 签署方若以代理人身份签署，须存在覆盖签署时点的授权
        if not sig.get("signer_as_agent_for"):
            return
        principal = sig["signer_as_agent_for"]
        ordered_at = self.order_event["ordered_at"] if self.order_event else sig["signed_at"]
        for ev in self.events:
            if (ev["type"] == "agency" and ev["principal"] == principal
                    and ev["agent"] == sig["signer_party"]
                    and ev["valid_from"] <= ordered_at <= ev["valid_to"]):
                scopes = ev["scope"]
                scopes = scopes if isinstance(scopes, list) else [scopes]
                if any("签署" in s or "订单" in s for s in scopes):
                    return
        self._freeze(f"签名证据 {sig['event_ref']} 的代理人缺乏覆盖该时点的有效授权")

    def _check_agency_conflict(self, agency: dict[str, Any]) -> None:
        for ev in self.events:
            if ev["type"] != "agency":
                continue
            if (ev["principal"] == agency["principal"] and ev["agent"] == agency["agent"]
                    and ev["scope"] != agency["scope"]
                    and self._periods_overlap(ev, agency)):
                self._freeze(
                    f"代理授权 {ev['event_ref']} 与 {agency['event_ref']} 范围冲突且有效期重叠"
                )

    def _freeze(self, reason: str) -> None:
        if reason not in self.freeze_reasons:
            self.freeze_reasons.append(reason)

    # ---------- 补充协议：只能改变尚未裁定、未被裁定使用的事项 ----------

    def _apply_supplement(self, event: dict[str, Any]) -> dict[str, Any]:
        matters = list(event["matters"])
        already = [m for m in matters if m in self.ruled_matters]
        if already:
            raise ValueError("补充协议不得改变已裁定事项：" + ",".join(already))
        changes = event["changes"]
        refs = self._refs_touched_by(changes)
        used = self._used_evidence_refs()
        touching_used = sorted(used.intersection(refs))
        if touching_used:
            raise ValueError("补充协议不得覆盖已在裁定中使用的证据：" + ",".join(touching_used))
        stored = self._store(event)
        stored["amends_refs"] = refs
        stored["accepted_matters"] = matters
        return stored

    def _used_evidence_refs(self) -> set[str]:
        used: set[str] = set()
        for ruling in self.rulings:
            used.update(ruling.evidence_refs)
        return used

    @staticmethod
    def _refs_touched_by(changes: dict[str, Any]) -> list[str]:
        refs: list[str] = []
        for value in changes.values():
            if isinstance(value, dict) and value.get("replaces_event_ref"):
                refs.append(value["replaces_event_ref"])
            elif isinstance(value, str) and value.startswith("EV-"):
                refs.append(value)
        return refs

    def _assert_statement_not_rewrite(self, incoming: dict[str, Any]) -> None:
        for ev in self.events:
            if ev["event_ref"] == incoming["event_ref"]:
                continue
            if (ev["type"] == "statement"
                    and ev["speaker_party"] == incoming["speaker_party"]
                    and ev.get("matter") == incoming.get("matter")
                    and ev["stated_at"] == incoming["stated_at"]
                    and ev["content"] != incoming["content"]):
                raise PermissionError(
                    f"当事人 {incoming['speaker_party']} 对事项 {incoming.get('matter')!r} "
                    f"在 {incoming['stated_at']} 的在先陈述不可被改写，"
                    "只能以新日期的陈述追加说明"
                )

    # ---------- 生命周期 ----------

    def enter_mediation(self, on: Optional[str] = None) -> None:
        if self.status not in (TRIAGE, INTAKE):
            raise ValueError(f"当前状态 {self.status} 不可进入调解")
        self._note_freeze_if_any()
        self.status = MEDIATION
        self._set_deadline("response", on)
        self._notify("调解受理通知", "response", on)
        self._raise_fee("调解服务费", 800.0, on)

    def enter_litigation(self, on: Optional[str] = None) -> None:
        if self.status not in (TRIAGE, MEDIATION):
            raise ValueError(f"当前状态 {self.status} 不可进入诉讼")
        self._note_freeze_if_any()
        self.status = LITIGATION
        self._set_deadline("answer", on)
        self._notify("诉讼应诉通知", "answer", on)
        self._raise_fee("立案费", 1200.0, on)

    def withdraw(self, on: Optional[str] = None) -> None:
        if self.status in (WITHDRAWN, RESOLVED):
            raise ValueError(f"当前状态 {self.status} 不可撤回")
        self.status = WITHDRAWN
        self._set_deadline("archive", on)
        self._notify("撤回确认通知", "archive", on)
        half = self._half_fee_due()
        if half is not None:
            self._raise_fee("撤回受理费（按标准减半）", half, on)

    def issue_ruling(self, *, matters: list[str], outcome: str,
                     evidence_refs: Optional[list[str]] = None,
                     issued_on: Optional[str] = None) -> Ruling:
        if self.status not in (MEDIATION, LITIGATION):
            raise ValueError(f"当前状态 {self.status} 不可出具裁定")
        if not matters:
            raise ValueError("裁定至少针对一个事项")
        known = set(self._known_matters())
        unknown = [m for m in matters if m not in known]
        if unknown:
            raise ValueError("裁定针对的事项没有事实/协议基础：" + ",".join(unknown))
        already = [m for m in matters if m in self.ruled_matters]
        if already:
            raise ValueError("以下事项已有生效裁定：" + ",".join(already))
        chain = self._build_evidence_chain(matters, evidence_refs)
        day = issued_on or self._today()
        record = self._audit(
            "system", "system", "view", chain, "出具裁定前核验证据链",
        )
        ruling = Ruling(
            ruling_id=f"RL-{len(self.rulings) + 1:03d}",
            issued_on=day,
            matters=list(matters),
            outcome=outcome,
            agency_event_ref=self._agency_ref_for_order(),
            rule_version=self.rule.version,
            evidence_refs=chain,
            audit_seq=record.seq,
        )
        self.rulings.append(ruling)
        for matter in matters:
            self.ruled_matters[matter] = ruling.ruling_id
        self._notify("裁定送达通知", None, issued_on, extra={"ruling_id": ruling.ruling_id})
        self._settle_fees(issued_on)
        if set(self.ruled_matters) == set(self._known_matters()):
            self.status = RESOLVED
            self._set_deadline("archive", issued_on)
        return ruling

    def _known_matters(self) -> list[str]:
        matters = ["合同成立"]  # 订单 + 签署即可审理合同成立
        for ev in self.events:
            if ev["type"] == "supplement":
                matters.extend(ev.get("accepted_matters", []))
            if ev.get("matter"):
                matters.append(ev["matter"])
        return sorted(set(matters))

    def _build_evidence_chain(self, matters: list[str],
                              specified: Optional[list[str]]) -> list[str]:
        refs: set[str] = set()
        if self.order_event:
            refs.add(self.order_event["event_ref"])
        for ev in self.events:
            if ev["type"] in ("agency", "signature", "payment", "jurisdiction", "consent"):
                refs.add(ev["event_ref"])
            if ev["type"] == "supplement" and any(m in matters for m in ev.get("accepted_matters", [])):
                refs.add(ev["event_ref"])
                refs.update(ev.get("amends_refs", []))
        if specified:
            known = {e["event_ref"] for e in self.events}
            unknown = [r for r in specified if r not in known]
            if unknown:
                raise ValueError("裁定引用了不存在的证据：" + ",".join(unknown))
            refs.update(specified)
        return sorted(refs)

    def _agency_ref_for_order(self) -> Optional[str]:
        """追溯触发争议交易的代理行为：下单时有效且范围覆盖订单的授权。"""
        if not self.order_event:
            return None
        ordered_at = self.order_event["ordered_at"]
        for ev in self.events:
            if ev["type"] != "agency":
                continue
            if ev["valid_from"] <= ordered_at <= ev["valid_to"]:
                scopes = ev["scope"]
                scopes = scopes if isinstance(scopes, list) else [scopes]
                if any("订单" in s for s in scopes):
                    return ev["event_ref"]
        return None

    # ---------- 期限 / 通知 / 费用 ----------

    def advance(self, today: Optional[str] = None) -> dict[str, Any]:
        """推进期限、通知与费用状态；进入调解/诉讼/撤回后仍持续推进。"""
        day = today or self._today()
        triggered: list[str] = []
        for name, due in self.deadlines.items():
            if day >= due:
                tag = f"{name} 期限已于 {due} 届满"
                if not any(tag in n["text"] for n in self.notices):
                    self._notify(tag, None, day)
                    triggered.append(tag)
        for fee in self.fees["records"]:
            if fee["status"] == "due" and day >= fee["raised_on"]:
                fee["status"] = "payable"
        return {"day": day, "triggered": triggered}

    def record_payment_of_fee(self, fee_name: str, paid_on: Optional[str] = None) -> None:
        day = paid_on or self._today()
        for fee in self.fees["records"]:
            if fee["name"] == fee_name and fee["status"] in ("due", "payable"):
                fee["status"] = "paid"
                fee["paid_on"] = day
                return
        raise ValueError(f"未找到待缴费用：{fee_name}")

    def _set_deadline(self, name: str, on: Optional[str]) -> None:
        base = _parse_date(on or self._today())
        self.deadlines[name] = (base + timedelta(days=DEADLINES[name])).isoformat()

    def _notify(self, text: str, deadline: Optional[str], on: Optional[str] = None,
                extra: Optional[dict[str, Any]] = None) -> None:
        self._notice_seq += 1
        notice: dict[str, Any] = {
            "seq": self._notice_seq,
            "text": text,
            "deadline_key": deadline,
            "due_on": self.deadlines.get(deadline) if deadline else None,
            "issued_on": on or self._today(),
        }
        if extra:
            notice.update(extra)
        self.notices.append(notice)

    def _raise_fee(self, name: str, amount: float, on: Optional[str]) -> None:
        self.fees["records"].append(
            {"name": name, "status": "due", "amount": amount,
             "raised_on": on or self._today()}
        )

    def _settle_fees(self, on: Optional[str]) -> None:
        for fee in self.fees["records"]:
            if fee["status"] in ("due", "payable"):
                fee["status"] = "assessed"
                fee["assessed_on"] = on or self._today()

    def _half_fee_due(self) -> Optional[float]:
        for fee in self.fees["records"]:
            if fee["name"] == "案件受理费" and fee["status"] in ("due", "payable"):
                return round(fee["amount"] / 2, 2)
        return None

    def _fee_for(self, event: dict[str, Any]) -> float:
        return 50.0 if event["amount"] <= self.rule.small_claim_threshold else 200.0

    def _note_freeze_if_any(self) -> None:
        # 矛盾冻结的是自动分流，不阻止当事人选择程序；进入程序时留痕提示人工核证。
        if self.freeze_reasons:
            tag = "材料矛盾待人工核证，自动分流冻结中"
            if not any(tag in n["text"] for n in self.notices):
                self._notify(tag, None)

    # ---------- 访问控制与审计 ----------

    def request_view(self, *, actor: str, role: Role,
                     event_refs: Optional[list[str]] = None,
                     purpose: str) -> list[dict[str, Any]]:
        """申请查看原件。

        - 调解员：任何一份原件未获授权即整体拒绝（只能查看获授权的原件）；
        - 当事人/运营者浏览（不指定编号）：仅返回有权查看的材料；
        - 明确指定编号却越权：拒绝，避免越权访问被静默过滤。
        """
        if _is_blank(purpose):
            raise ValueError("查看原件必须说明用途 purpose")
        candidates = list(self.events) if event_refs is None else self._resolve_refs(event_refs)
        visible, denied = [], []
        for ev in candidates:
            ok, why = self._can_view(role, actor, ev)
            if ok:
                visible.append(ev)
            else:
                denied.append(f"{ev['event_ref']}({why})")
        self._audit(actor, role, "view", [ev["event_ref"] for ev in candidates], purpose,
                    granted=not denied,
                    reason_if_denied=None if not denied else ";".join(denied))
        if denied and (role == "mediator" or event_refs is not None):
            prefix = "调解员只能查看获授权的原件：" if role == "mediator" else "存在无权查看的原件："
            raise PermissionError(prefix + ";".join(denied))
        return visible

    def transfer(self, *, actor: str, role: Role, destination: str,
                 event_refs: list[str], purpose: str) -> None:
        """跨境转交：逐份核对数据出境同意；缺同意则整体拒绝并留痕。"""
        if _is_blank(purpose) or _is_blank(destination):
            raise ValueError("跨境转交必须注明接收方 destination 与原因 purpose")
        events = self._resolve_refs(event_refs)
        blocked: list[str] = []
        for ev in events:
            cats = self._categories_of(ev)
            if self.rule.data_export_requires_consent and not self._consent_covers(ev, cats, destination):
                blocked.append(f"{ev['event_ref']}[缺 {','.join(cats)} 出境同意]")
        self._audit(actor, role, "transfer", [ev["event_ref"] for ev in events], purpose,
                    destination=destination, granted=not blocked,
                    reason_if_denied=None if not blocked else ";".join(blocked))
        if blocked:
            raise PermissionError("以下材料缺失数据出境同意，不得转交：" + ";".join(blocked))

    def submit_fact(self, *, role: Role, actor: str,
                    event: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """平台运营者可补充交易事实（fact）；陈述只能由当事人本人提交，运营者无权改写。"""
        if role not in ("operator", "party"):
            raise PermissionError("该角色不得提交材料")
        if event.get("type") == "statement" and role != "party":
            raise PermissionError("平台运营者无权改写或代交当事人陈述")
        if role == "operator" and event.get("type") != "fact":
            raise PermissionError("平台运营者仅可补充交易事实 fact")
        if role == "party" and event.get("type") == "statement":
            event = dict(event, speaker_party=actor)
        return self.receive(event)

    def _can_view(self, role: str, actor: str, ev: dict[str, Any]) -> tuple[bool, str]:
        if role == "mediator":
            if self._mediator_authorized(ev):
                return True, ""
            return False, "调解员未获该原件授权"
        owner = self._owner_of(ev)
        if owner is not None and owner == actor:
            return True, ""
        if owner is None:
            return True, ""  # 管辖约定、补充协议等共同材料对当事人可见
        return False, f"仅 {owner} 可查看"

    def _mediator_authorized(self, ev: dict[str, Any]) -> bool:
        cats = self._categories_of(ev)
        return any(
            g["type"] == "consent"
            and self._consent_covers_event(g, ev, cats, mediator_access=True)
            for g in self.events
        )

    def _consent_covers(self, ev: dict[str, Any], cats: list[str], destination: str) -> bool:
        return any(
            g["type"] == "consent"
            and self._consent_covers_event(g, ev, cats, destination=destination)
            for g in self.events
        )

    def _consent_covers_event(self, grant: dict[str, Any], ev: dict[str, Any], cats: list[str],
                              destination: Optional[str] = None,
                              mediator_access: bool = False) -> bool:
        owner = self._owner_of(ev)
        if owner is not None and grant["granting_party"] != owner:
            return False  # 仅数据所有者本人可授权其原件
        if not _list_overlap(grant["data_categories"], cats):
            return False
        if mediator_access:
            return bool(grant.get("allow_mediator_view", False))
        if grant.get("valid_to"):
            valid_from = grant.get("valid_from", grant["granted_at"])
            if not (valid_from <= self._today() <= grant["valid_to"]):
                return False
        return destination in grant["destinations"]

    def _audit(self, actor: str, role: str, action: str, refs: list[str],
               purpose: str, destination: Optional[str] = None,
               granted: bool = True, reason_if_denied: Optional[str] = None) -> AccessRecord:
        self._access_seq += 1
        record = AccessRecord(
            seq=self._access_seq, ts=self.clock().isoformat(), actor=actor, role=role,
            action=action, event_refs=list(refs), purpose=purpose,
            destination=destination, granted=granted, reason_if_denied=reason_if_denied,
            rule_version=self.rule.version,
        )
        self.access_log.append(record)
        return record

    def _resolve_refs(self, refs: list[str]) -> list[dict[str, Any]]:
        by_ref = {e["event_ref"]: e for e in self.events}
        missing = [r for r in refs if r not in by_ref]
        if missing:
            raise ValueError("证据编号不存在：" + ",".join(missing))
        return [by_ref[r] for r in refs]

    @staticmethod
    def _owner_of(ev: dict[str, Any]) -> Optional[str]:
        mapping = {
            "order": "actor_party",
            "payment": "payer_party",
            "signature": "signer_party",
            "statement": "speaker_party",
            "consent": "granting_party",
            "agency": "principal",
        }
        key = mapping.get(ev["type"])
        return ev[key] if key else None

    @staticmethod
    def _categories_of(ev: dict[str, Any]) -> list[str]:
        explicit = ev.get("data_categories")
        if isinstance(explicit, list) and explicit:
            return explicit
        mapping = {
            "order": ["交易信息"],
            "payment": ["支付信息"],
            "signature": ["合同信息", "身份信息"],
            "consent": ["同意记录", "交易信息", "支付信息", "合同信息", "身份信息"],
            "statement": ["陈述信息"],
            "agency": ["身份信息", "代理信息"],
            "jurisdiction": ["合同信息"],
            "supplement": ["合同信息"],
            "fact": ["交易信息"],
        }
        return mapping.get(ev["type"], ["其他信息"])

    # ---------- 快照 / 恢复 / 溯源 ----------

    def to_snapshot(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "rule_version": self.rule.version,
            "rule_effective_date": self.rule.effective_date.isoformat(),
            "status": self.status,
            "route_queue": self.route_queue,
            "freeze_reasons": list(self.freeze_reasons),
            "events": [dict(ev) for ev in self.events],
            "deadlines": dict(self.deadlines),
            "notices": [dict(n) for n in self.notices],
            "fees": {"currency": self.fees["currency"],
                     "records": [dict(f) for f in self.fees["records"]]},
            "rulings": [r.__dict__ for r in self.rulings],
            "ruled_matters": dict(self.ruled_matters),
            "access_log": [a.__dict__ for a in self.access_log],
            "counters": {"event": self._event_seq, "notice": self._notice_seq,
                         "access": self._access_seq},
        }

    @classmethod
    def from_snapshot(cls, snap: dict[str, Any], rule: RuleSet,
                      clock: Callable[[], datetime] = _utcnow) -> "DisputeCaseLog":
        if snap["rule_version"] != rule.version:
            raise ValueError(
                f"恢复所用规则版本 {rule.version} 与案件适用版本 {snap['rule_version']} 不符；"
                "必须按案件发生时有效的规则版本恢复"
            )
        case = cls(rule, clock=clock)
        case.case_id = snap["case_id"]
        case.status = snap["status"]
        case.route_queue = snap["route_queue"]
        case.freeze_reasons = list(snap["freeze_reasons"])
        case.events = [dict(ev) for ev in snap["events"]]
        case._by_fingerprint = {ev["fingerprint"]: ev for ev in case.events}
        case.order_event = next((ev for ev in case.events if ev["type"] == "order"), None)
        case.deadlines = dict(snap["deadlines"])
        case.notices = [dict(n) for n in snap["notices"]]
        case.fees = {"currency": snap["fees"]["currency"],
                     "records": [dict(f) for f in snap["fees"]["records"]]}
        case.rulings = [Ruling(**r) for r in snap["rulings"]]
        case.ruled_matters = dict(snap["ruled_matters"])
        case.access_log = [AccessRecord(**a) for a in snap["access_log"]]
        counters = snap.get("counters", {})
        case._event_seq = counters.get("event", len(case.events))
        case._notice_seq = counters.get("notice", len(case.notices))
        case._access_seq = counters.get("access", len(case.access_log))
        return case

    def trace_ruling(self, ruling_id: str) -> dict[str, Any]:
        """从一项裁定追溯触发它的代理行为、规则依据和完整证据链。"""
        ruling = next((r for r in self.rulings if r.ruling_id == ruling_id), None)
        if ruling is None:
            raise ValueError(f"裁定不存在：{ruling_id}")
        agency = next(
            (ev for ev in self.events if ev["event_ref"] == ruling.agency_event_ref), None
        )
        return {
            "ruling": ruling.__dict__,
            "agency_action": agency,
            "rule_basis": {
                "version": self.rule.version,
                "effective_date": self.rule.effective_date.isoformat(),
                "small_claim_threshold": self.rule.small_claim_threshold,
                "data_export_requires_consent": self.rule.data_export_requires_consent,
            },
            "evidence_chain": self._resolve_refs(ruling.evidence_refs),
            "audit_at_issue": next(
                (a.__dict__ for a in self.access_log if a.seq == ruling.audit_seq), None
            ),
        }

    # ---------- 工具 ----------

    def _today(self) -> str:
        return self.clock().date().isoformat()

    def _payment_total(self, pay: dict[str, Any]) -> float:
        parts = pay.get("settlements")
        if isinstance(parts, list) and parts:
            return round(sum(float(p["amount"]) for p in parts), 2)
        return float(pay["amount"])

    @staticmethod
    def _periods_overlap(a: dict[str, Any], b: dict[str, Any]) -> bool:
        return a["valid_from"] <= b["valid_to"] and b["valid_from"] <= a["valid_to"]


def _amounts_equal(a: float, b: float) -> bool:
    return abs(float(a) - float(b)) < 0.01


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _list_overlap(a: list[str], b: list[str]) -> bool:
    return bool(set(a) & set(b))


@dataclass
class DisputeService:
    """争议归档服务：多案登记、指纹去重、规则快照与可复核争议包。"""

    rule: RuleSet = field(default_factory=RuleSet.v1)
    clock: Callable[[], datetime] = _utcnow
    cases: dict[str, DisputeCaseLog] = field(default_factory=dict)
    _fingerprint_index: dict[str, str] = field(default_factory=dict)
    _pending: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def ingest(self, event: dict[str, Any]) -> tuple[DisputeCaseLog, str]:
        """接收一条事件。首条须为订单；相同事件重复提交返回原案件。

        返回 (案件, 动作)，动作为 created / duplicated / received。
        非订单事件需带 order_ref；订单尚未立案时按 order_ref 暂存，
        立案后按到达顺序补登记，以容纳授权早于订单、支付晚于争议等乱序场景。
        """
        validate_event(event)
        fp = event_fingerprint(event)
        if fp in self._fingerprint_index:
            case = self.cases[self._fingerprint_index[fp]]
            if event["type"] == "order":
                raise DuplicateEvent(f"订单 {event['order_ref']} 已立案为 {case.case_id}")
            return case, "duplicated"
        if event["type"] == "order":
            case = DisputeCaseLog(self.rule, clock=self.clock)
            case.open_case(event)
            self.cases[case.case_id] = case
            self._fingerprint_index[fp] = case.case_id
            for buffered in self._pending.pop(event["order_ref"], []):
                if event_fingerprint(buffered) in self._fingerprint_index:
                    continue
                stored, _ = case.receive(buffered)
                self._fingerprint_index[stored["fingerprint"]] = case.case_id
            return case, "created"
        order_ref = event.get("order_ref")
        if not order_ref:
            case = self._only_case()
        elif order_ref in self._cases_by_order_ref():
            case = self._require_case(order_ref)
        else:
            # 订单尚未立案：暂存，等待订单事件到达；暂存队列内同指纹视为重复
            buffered = self._pending.setdefault(order_ref, [])
            if any(event_fingerprint(e) == fp for e in buffered):
                raise DuplicateEvent(f"订单 {order_ref} 尚未立案，事件 {fp[:12]} 已在暂存队列")
            buffered.append(event)
            raise PendingOrder(order_ref)
        stored, ignored = case.receive(event)
        if not ignored:
            self._fingerprint_index[fp] = case.case_id
        return case, "duplicated" if ignored else "received"

    def _cases_by_order_ref(self) -> dict[str, DisputeCaseLog]:
        return {c.order_event["order_ref"]: c
                for c in self.cases.values() if c.order_event}

    def _only_case(self) -> DisputeCaseLog:
        if len(self.cases) != 1:
            raise ValueError("非订单事件必须带 order_ref 以定位案件")
        return next(iter(self.cases.values()))

    def _require_case(self, order_ref: str) -> DisputeCaseLog:
        for case in self.cases.values():
            if case.order_event and case.order_event["order_ref"] == order_ref:
                return case
        raise ValueError(f"订单 {order_ref} 未立案")

    def get_case(self, case_id: str) -> DisputeCaseLog:
        if case_id not in self.cases:
            raise ValueError(f"案件不存在：{case_id}")
        return self.cases[case_id]

    def build_package(self, case_id: str) -> dict[str, Any]:
        """生成可复核争议包：规则快照、事件、冻结标记、审计日志与溯源索引。"""
        case = self.get_case(case_id)
        return {
            "case_id": case.case_id,
            "generated_at": self.clock().isoformat(),
            "rule_snapshot": {
                "version": case.rule.version,
                "effective_date": case.rule.effective_date.isoformat(),
                "small_claim_threshold": case.rule.small_claim_threshold,
                "data_export_requires_consent": case.rule.data_export_requires_consent,
                "required_evidence": list(case.rule.required_evidence),
            },
            "summary": {
                "status": case.status,
                "route_queue": case.route_queue,
                "frozen": case.frozen,
                "freeze_reasons": case.freeze_reasons,
                "event_count": len(case.events),
                "ruling_count": len(case.rulings),
            },
            "events": [dict(ev) for ev in case.events],
            "agreements": {
                "supplements": [dict(e) for e in case.events if e["type"] == "supplement"],
                "ruled_matters": dict(case.ruled_matters),
            },
            "deadlines_notices_fees": {
                "deadlines": dict(case.deadlines),
                "notices": [dict(n) for n in case.notices],
                "fees": {"currency": case.fees["currency"],
                         "records": [dict(f) for f in case.fees["records"]]},
            },
            "access_and_transfer_log": [a.__dict__ for a in case.access_log],
            "rulings": [r.__dict__ for r in case.rulings],
            "integrity": {"event_fingerprints": sorted(case._by_fingerprint)},
        }


def _load_fixture(path: str) -> tuple[DisputeService, DisputeCaseLog]:
    """从演示资料文件按序摄取事件，返回服务与案件（用于命令行复核）。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    service = DisputeService()
    case: Optional[DisputeCaseLog] = None
    for event in data["events"]:
        try:
            case, _ = service.ingest(event)
        except PendingOrder:
            continue
    if case is None:
        raise ValueError("资料中没有任何订单事件，无法立案")
    return service, case


def summarize_case(case: DisputeCaseLog) -> str:
    return (
        f"{case.case_id}｜规则 {case.rule.version}｜状态 {case.status}"
        f"｜分流 {case.route_queue or '未定'}｜材料 {len(case.events)} 份"
        f"｜{'已冻结：' + '；'.join(case.freeze_reasons) if case.frozen else '自动分流未冻结'}"
    )


if __name__ == "__main__":  # pragma: no cover
    import sys

    if len(sys.argv) not in (2, 3):
        raise SystemExit("用法：python3 -m src.trade_expo_collaboration.disputes <案件资料.json> [争议包输出.json]")
    svc, case_log = _load_fixture(sys.argv[1])
    package = svc.build_package(case_log.case_id)
    if len(sys.argv) == 3:
        Path(sys.argv[2]).write_text(
            json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(summarize_case(case_log) + f"｜争议包已写入 {sys.argv[2]}")
    else:
        print(summarize_case(case_log))
