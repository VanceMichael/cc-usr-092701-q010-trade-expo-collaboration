"""跨境数字订单争议归档与分流服务。

仅依赖标准库。核心不变量：

* 所有状态变更先写入哈希链的仅追加日志，再派生内存状态，恢复时重放即可；
* 案件适用其“发生时”有效的规则版本，版本一经确定不再漂移；
* 重复事件按事件编号幂等返回原案件；
* 签名或支付材料互相矛盾时冻结自动分流，冻结具有黏性；
* 证据只追加，后续协议只能影响尚未裁定的事项；
* 每次原件访问与跨境转交都留痕，原因必填。
"""

from .errors import (
    AuthorizationError,
    ChainIntegrityError,
    ImmutabilityError,
    IntakeValidationError,
    RoutingBlocked,
    RuleVersionError,
)
from .journal import Journal
from .rules import RuleRegistry, RuleVersion, build_default_registry
from .service import DisputeService
from .package import build_package, trace_decision

__all__ = [
    "AuthorizationError",
    "ChainIntegrityError",
    "ImmutabilityError",
    "IntakeValidationError",
    "RoutingBlocked",
    "RuleVersionError",
    "Journal",
    "RuleRegistry",
    "RuleVersion",
    "build_default_registry",
    "DisputeService",
    "build_package",
    "trace_decision",
]
