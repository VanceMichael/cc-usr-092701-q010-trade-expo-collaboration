"""争议入站事件模型与字段校验。

七类业务事件对应需求：订单事件、代理授权、签名证据、数据处理同意、
支付流水、管辖约定、当事人陈述。其余为服务在处理过程中产生的派生/管理事件。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import IntakeValidationError

# ---- 业务事件（外部提交） -------------------------------------------------
ORDER_EVENT = "order_event"
AGENT_AUTHORIZATION = "agent_authorization"
SIGNATURE_EVIDENCE = "signature_evidence"
DATA_CONSENT = "data_consent"
PAYMENT_RECORD = "payment_record"
JURISDICTION_AGREEMENT = "jurisdiction_agreement"
PARTY_STATEMENT = "party_statement"
OPERATOR_FACT = "operator_fact"
SUPPLEMENTAL_AGREEMENT = "supplemental_agreement"

BUSINESS_TYPES = frozenset(
    {
        ORDER_EVENT,
        AGENT_AUTHORIZATION,
        SIGNATURE_EVIDENCE,
        DATA_CONSENT,
        PAYMENT_RECORD,
        JURISDICTION_AGREEMENT,
        PARTY_STATEMENT,
        OPERATOR_FACT,
        SUPPLEMENTAL_AGREEMENT,
    }
)

# ---- 派生/管理事件（服务生成） --------------------------------------------
CASE_OPENED = "case_opened"
ROUTING_DECIDED = "routing_decided"
AUTO_FROZEN = "auto_frozen"
DECISION_MADE = "decision_made"
CASE_STATUS = "case_status"
DEADLINE_UPDATE = "deadline_update"
NOTICE_SENT = "notice_sent"
FEE_UPDATE = "fee_update"
DATA_ACCESS = "data_access"
DATA_TRANSFER = "data_transfer"
GRANT_ADDED = "grant_added"

_REQUIRED: dict[str, tuple[str, ...]] = {
    ORDER_EVENT: ("order_id", "occurred_at", "buyer", "seller", "amount", "currency"),
    AGENT_AUTHORIZATION: ("order_id", "grantor", "agent", "scope", "valid_from", "valid_to"),
    SIGNATURE_EVIDENCE: ("order_id", "doc_ref", "signer", "method", "status"),
    DATA_CONSENT: ("order_id", "consent_id", "subject", "purposes", "destinations", "scopes", "granted"),
    PAYMENT_RECORD: ("order_id", "institution", "status", "amount", "currency"),
    JURISDICTION_AGREEMENT: ("order_id", "forum", "governing_law"),
    PARTY_STATEMENT: ("order_id", "party", "text"),
    OPERATOR_FACT: ("order_id", "text"),
    SUPPLEMENTAL_AGREEMENT: ("order_id", "changes"),
}

_VALID_SIGNATURE_STATUS = frozenset({"valid", "invalid", "mismatch"})
_PAID_STATUS = frozenset({"paid", "settled"})
_UNPAID_STATUS = frozenset({"unpaid", "failed", "refunded", "returned"})


@dataclass(frozen=True)
class IncomingEvent:
    event_id: str
    event_type: str
    timestamp: str
    payload: dict[str, Any]


def validate_event(value: Any) -> IncomingEvent:
    if not isinstance(value, dict):
        raise IntakeValidationError("事件必须是对象")
    for field_name in ("event_id", "event_type", "timestamp", "payload"):
        if field_name not in value:
            raise IntakeValidationError(f"事件缺少字段：{field_name}")
    event_id = value["event_id"]
    event_type = value["event_type"]
    timestamp = value["timestamp"]
    payload = value["payload"]
    if not isinstance(event_id, str) or not event_id.strip():
        raise IntakeValidationError("event_id 必须是非空文本")
    if not isinstance(timestamp, str) or not timestamp.strip():
        raise IntakeValidationError("timestamp 必须是非空文本")
    if event_type not in BUSINESS_TYPES:
        raise IntakeValidationError(f"未知事件类型：{event_type}")
    if not isinstance(payload, dict):
        raise IntakeValidationError("payload 必须是对象")
    if "order_id" not in payload or not str(payload.get("order_id", "")).strip():
        raise IntakeValidationError("payload 必须包含非空 order_id")
    for name in _REQUIRED[event_type]:
        if name not in payload or payload[name] in (None, ""):
            raise IntakeValidationError(f"{event_type} 缺少字段：{name}")
    _validate_semantics(event_type, payload)
    return IncomingEvent(event_id=event_id, event_type=event_type, timestamp=timestamp, payload=payload)


def _validate_semantics(event_type: str, p: dict) -> None:
    if event_type == ORDER_EVENT:
        if not isinstance(p["amount"], (int, float)) or p["amount"] <= 0:
            raise IntakeValidationError("订单金额必须为正数")
        if not isinstance(p.get("occurred_at", ""), str):
            raise IntakeValidationError("occurred_at 必须是文本时间")
    elif event_type == SIGNATURE_EVIDENCE:
        if p["status"] not in _VALID_SIGNATURE_STATUS:
            raise IntakeValidationError(f"签名状态只能是 {sorted(_VALID_SIGNATURE_STATUS)}")
    elif event_type == PAYMENT_RECORD:
        if p["status"] not in _PAID_STATUS | _UNPAID_STATUS:
            raise IntakeValidationError("支付状态不被识别")
        if not isinstance(p["amount"], (int, float)) or p["amount"] < 0:
            raise IntakeValidationError("支付金额必须是非负数")
    elif event_type == DATA_CONSENT:
        for name in ("purposes", "destinations", "scopes"):
            if not isinstance(p[name], list) or not all(isinstance(x, str) and x for x in p[name]):
                raise IntakeValidationError(f"同意的 {name} 必须是非空文本列表")
        if not isinstance(p["granted"], bool):
            raise IntakeValidationError("granted 必须是布尔值")
    elif event_type == SUPPLEMENTAL_AGREEMENT:
        changes = p["changes"]
        if not isinstance(changes, list) or not changes:
            raise IntakeValidationError("补充协议必须包含至少一项变更")
        for change in changes:
            if not isinstance(change, dict) or not change.get("matter"):
                raise IntakeValidationError("每项变更必须指定 matter")
