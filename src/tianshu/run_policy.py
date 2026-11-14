"""创建前固定的 Run 执行资格；不授予工具权限。"""

from collections.abc import Mapping
from dataclasses import dataclass

from .errors import TianshuError, ToolNotAllowed


class RunEffectsDisallowed(ToolNotAllowed):
    rule_id = "dispatch.run_effects_disallowed"


@dataclass(frozen=True)
class RunExecutionPolicyV1:
    format_version: int
    mode: str

    def __post_init__(self) -> None:
        if type(self.format_version) is not int or self.format_version != 1:
            raise TianshuError("Run execution_policy.format_version 必须为整数 1")
        if self.mode not in ("pure_only", "effects"):
            raise TianshuError(f"未知 Run execution_policy.mode：{self.mode!r}")

    @classmethod
    def of(cls, value: Mapping[str, object]) -> "RunExecutionPolicyV1":
        if not isinstance(value, Mapping) or set(value) != {"format_version", "mode"}:
            raise TianshuError("Run execution_policy 字段必须精确为 format_version/mode")
        return cls(value["format_version"], value["mode"])

    def to_dict(self) -> dict[str, object]:
        return {"format_version": self.format_version, "mode": self.mode}


EFFECTS = RunExecutionPolicyV1(1, "effects")
PURE_ONLY = RunExecutionPolicyV1(1, "pure_only")
