"""争议服务的错误类型。"""


class DisputeError(Exception):
    """争议服务错误基类。"""


class IntakeValidationError(DisputeError):
    """事件入站结构或字段不合法。"""


class RuleVersionError(DisputeError):
    """找不到案件发生时有效的规则版本。"""


class RoutingBlocked(DisputeError):
    """自动分流无法进行（材料矛盾冻结或管辖约定冲突）。

    ``code`` 取值：``frozen``（签名/支付矛盾冻结）、``jurisdiction-conflict``。
    """

    def __init__(self, message: str, code: str, reasons: list[str] | None = None):
        super().__init__(message)
        self.code = code
        self.reasons = reasons or []


class AuthorizationError(DisputeError):
    """角色无权执行该操作，或缺少原件访问授权/出境依据。"""


class ImmutabilityError(DisputeError):
    """试图覆盖已裁定事项或已被裁定使用的证据。"""


class ChainIntegrityError(DisputeError):
    """日志哈希链校验失败，存在被篡改或缺失的条目。"""
