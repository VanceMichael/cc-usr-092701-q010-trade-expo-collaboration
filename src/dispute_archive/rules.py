"""版本化规则注册表。

案件适用“发生时”有效的规则版本：根据订单事件时间选出当时生效且尚未废止
的版本，之后即使发布新版本，已建案件的依据也保持不变（可追溯、可复核）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .errors import RuleVersionError


@dataclass(frozen=True)
class RuleVersion:
    version: str
    effective_from: str
    rules: tuple[str, ...]
    effective_until: str | None = None  # None 表示持续有效
    notes: str = ""

    def in_force_at(self, when: str) -> bool:
        if when < self.effective_from:
            return False
        return self.effective_until is None or when < self.effective_until


@dataclass
class RuleRegistry:
    _versions: list[RuleVersion] = field(default_factory=list)

    def register(self, rv: RuleVersion) -> "RuleRegistry":
        if any(v.version == rv.version for v in self._versions):
            raise ValueError(f"规则版本已存在：{rv.version}")
        self._versions.append(rv)
        self._versions.sort(key=lambda v: v.effective_from)
        return self

    def versions(self) -> tuple[RuleVersion, ...]:
        return tuple(self._versions)

    def version_in_force_at(self, when: str) -> RuleVersion:
        """返回 ``when`` 时刻有效的最新版本。"""
        candidates = [v for v in self._versions if v.in_force_at(when)]
        if not candidates:
            raise RuleVersionError(f"{when} 没有有效的规则版本")
        return max(candidates, key=lambda v: v.effective_from)

    def get(self, version: str) -> RuleVersion:
        for v in self._versions:
            if v.version == version:
                return v
        raise RuleVersionError(f"未知规则版本：{version}")


# 默认规则集为演示用虚构条款，编号即规则编号，供裁定依据引用。
_DEFAULT_BUILDERS: Callable[[], list[RuleVersion]] = lambda: [
    RuleVersion(
        version="DTE-2024.1",
        effective_from="2024-01-01T00:00:00Z",
        rules=(
            "R1 智能助手在授权范围内以本人名义下单，订单到达相对方系统时合同成立",
            "R2 可靠电子签名与手写签名具有同等法律效力，签名证据须可验证签署主体与时间",
            "R3 持牌结算机构出具的支付流水可证明付款状态，资金路径以流水记载为准",
            "R4 个人数据出境须具备明确同意或其他法定依据，访问与转交须留存目的记录",
            "R5 平台、商家与技术服务商按各自对订单环节的控制与过错承担相应责任",
            "R6 当事人有效管辖约定优先；约定冲突时暂停自动分流",
        ),
        notes="数贸会跨境小额数字订单争议规则首版（虚构演示）",
    ),
    RuleVersion(
        version="DTE-2025.2",
        effective_from="2025-06-01T00:00:00Z",
        rules=(
            "R1 智能助手在授权范围内以本人名义下单，订单到达相对方系统时合同成立",
            "R2 可靠电子签名须附带签署设备与证书链信息方可作为成立证据",
            "R3 持牌结算机构出具的支付流水可证明付款状态；多机构流水冲突时不得自动认定已付",
            "R4 个人数据出境须具备明确同意或其他法定依据，且每次转交须记录最小必要范围",
            "R5 平台、商家与技术服务商按各自对订单环节的控制与过错承担相应责任",
            "R6 当事人有效管辖约定优先；约定冲突时暂停自动分流",
            "R7 补充协议仅对尚未裁定的事项生效，不得覆盖已使用的证据",
        ),
        notes="强化签名证书链、支付冲突与补充协议边界（虚构演示）",
    ),
    RuleVersion(
        version="DTE-2026.1",
        effective_from="2026-03-01T00:00:00Z",
        rules=(
            "R1 智能助手在授权范围内以本人名义下单，订单到达相对方系统时合同成立；越权下单按表见代理处理",
            "R2 可靠电子签名须附带签署设备与证书链信息，并完成签署意愿二次校验",
            "R3 持牌结算机构出具的支付流水可证明付款状态；多机构流水冲突时不得自动认定已付",
            "R4 个人数据出境须具备明确同意或其他法定依据，且每次转交须记录最小必要范围",
            "R5 平台、商家与技术服务商按各自对订单环节的控制与过错承担相应责任",
            "R6 当事人有效管辖约定优先；约定冲突时暂停自动分流",
            "R7 补充协议仅对尚未裁定的事项生效，不得覆盖已使用的证据",
            "R8 争议进入调解、诉讼或撤回后，期限、通知与费用状态仍须继续推进留痕",
        ),
        notes="增加表见代理与结案后事项推进要求（虚构演示）",
    ),
]


def build_default_registry() -> RuleRegistry:
    registry = RuleRegistry()
    for rv in _DEFAULT_BUILDERS():
        registry.register(rv)
    return registry
