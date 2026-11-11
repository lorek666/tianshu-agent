"""工具参数的**封闭** JSON Schema 子集：编译期拒绝，执行期逐层校验。

## 为什么要有这一层

PRD §5.2.3 要求「执行侧验证所声明的**完整** schema，包括嵌套对象、数组和约束；
不支持的 dialect / 关键字拒绝准入，不忽略」。此前的 `tools.validate_args()` 只做
顶层 `type / required / additionalProperties`，而 `SPECS` 里已经写着
`argv: {items: {type: string}, minItems: 1}`、`limit: {minimum: 1, maximum: 5}`
——这些约束**从未被执行过**。也就是说：契约里写了，执行侧不看。模型给
`argv: [1]` 或 `limit: 99` 时，旧代码直接放行，问题要到工具实现里才炸（或者
根本不炸）。这比没有 schema 更糟：它让人以为契约被强制了。

## 为什么"拒绝"而不是"忽略"未支持的关键字

JSON Schema 的未实现关键字按规范是**注解**，忽略它并不违反规范。但这不适用于
授权边界：注册表里写着 `oneOf`，执行侧却只认 `type`，等于对模型和审批人一起
撒谎（"这个参数形状被检查过"）。所以本模块的方言是**封闭白名单**：多一个关键字
就在注册期报错，逼作者显式删掉或先实现它。`$ref` 更是必须拒绝——解析外部引用
意味着联网或读文件，PRD 明确禁止。

## 方言（多一个都不认）

| 位置 | 允许 |
|---|---|
| 根 / 对象 | `type: object`、`properties`、`required`、
  `additionalProperties`（只允许 bool）、`title`、`description` |
| 数组 | `type: array`、`items`（单个 schema，不支持 tuple）、`minItems`、`maxItems` |
| 标量 | `type: string|integer|number|boolean`、`enum`、`minimum`、
  `maximum`、`minLength`、`maxLength`、`pattern` |

`minimum` / `maximum` 只接受整数：本项目所有授权参数都是整数，混进浮点会让
"摘要"与"比较"两套语义分叉（见 `canonical.py` 为什么拒绝浮点）。

## 与既有行为的关系

`tools.validate_args()` 仍是公开入口，签名与异常类型不变（`ToolArgumentError`），
只是内部改成"编译（带缓存）+ 校验"。所以 `loop.py` / `memory_exec.py` 一行不用改，
而嵌套约束从"写着"变成"真的执行"。老消息格式（顶层缺少必填 / 多余参数 / 类型不符）
保持原样，避免既有测试与日志口径被打断；新增的是**嵌套路径**。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from .errors import ToolArgumentError, ToolSchemaError

#: 模型**绝对不能**通过工具参数指定的身份字段（PRD §5.2.3：执行上下文由系统提供
#: task / run / call / attempt、deadline、取消状态和预算账户）。
#:
#: 注册期拒绝声明，执行期拒绝传入。两道都要：只做注册期，将来某个工具放宽成
#: `additionalProperties: true` 就把身份字段又带回来了；只做执行期，则 schema 本身
#: 在骗人（写着这个参数合法）。
MODEL_FORBIDDEN_ARGS: frozenset[str] = frozenset({
    "approval_id",
    "attempt_id",
    "budget",
    "budget_account",
    "call_id",
    "claim_epoch",
    "deadline",
    "dispatch_id",
    "goal_revision",
    "grant_id",
    "job_id",
    "lease_epoch",
    "owner_instance_id",
    "request_id",
    "run_id",
    "task_id",
})

#: 允许出现在任何 schema 节点的**注解**关键字。它们不影响校验，
#: 但作者写了就应该被保留（`description` 会进工具清单给人看）。
_ANNOTATIONS = frozenset({"title", "description"})

_OBJECT_KEYS = frozenset({"type", "properties", "required", "additionalProperties"})
_ARRAY_KEYS = frozenset({"type", "items", "minItems", "maxItems"})
_SCALAR_KEYS = frozenset({
    "type",
    "enum",
    "minimum",
    "maximum",
    "minLength",
    "maxLength",
    "pattern",
})

_TYPES = frozenset({"object", "array", "string", "integer", "number", "boolean"})

#: 递归上限。注册期的 schema 是仓库里的常量，不是用户输入；给一个上限只是
#: 为了让"有人手写一个 1000 层嵌套"时得到明确错误，而不是 RecursionError。
MAX_DEPTH = 12

#: 编译结果缓存：schema 是 dict（不可哈希），所以用规范 JSON 串做键。
_CACHE: dict[str, CompiledSchema] = {}


def _path(*parts: object) -> str:
    """JSON 路径的紧凑写法：`argv[0].name`；根用 `参数`。"""
    out = ""
    for part in parts:
        if isinstance(part, int):
            out += f"[{part}]"
        elif out:
            out += f".{part}"
        else:
            out = str(part)
    return f"参数 {out}" if out else "参数"


def canonical_digest(payload: Any) -> str:
    """与派发侧 `schema_hash` 同一个算法（键排序、无空白、保留中文）。"""
    import json

    raw = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class CompiledSchema:
    """一个编译好的参数 schema。`validate()` 是执行侧唯一入口。"""

    schema: dict[str, Any]
    digest: str
    _root: _Node

    @property
    def root_type(self) -> str:
        return self._root.kind

    def validate(self, args: Any) -> dict[str, Any]:
        if not isinstance(args, dict):
            raise ToolArgumentError(
                f"参数必须是对象，实际是 {type(args).__name__}"
            )
        # 身份字段的第二道闸：即使某个工具（将来）放宽成
        # `additionalProperties: true`，模型也不能把 run/attempt/预算身份
        # 塞进参数里假装自己是系统。真正的原因不在这一层——没有任何代码
        # 从参数读身份——但"schema 允许"和"执行侧接受"必须口径一致。
        forbidden = sorted(set(args) & MODEL_FORBIDDEN_ARGS)
        if forbidden:
            raise ToolArgumentError(
                f"参数里出现执行身份字段 {forbidden}：task/run/call/attempt、"
                "deadline 与预算账户只能由系统提供，模型不得指定"
            )
        self._root.check(args, _path())
        return args


@dataclass(frozen=True)
class _Node:
    kind: str
    #: 对象节点：已编译的属性表；数组节点为空。
    properties: tuple[tuple[str, _Node], ...] = ()
    required: tuple[str, ...] = ()
    #: `None` = 未声明（按 JSON Schema 默认允许额外键）；`False` = 禁止。
    additional: bool | None = None
    items: _Node | None = None
    min_items: int | None = None
    max_items: int | None = None
    enum: tuple[Any, ...] | None = None
    minimum: int | None = None
    maximum: int | None = None
    min_length: int | None = None
    max_length: int | None = None
    pattern: re.Pattern[str] | None = None

    # ------------------------------------------------------------ 校验

    def check(self, value: Any, where: str) -> None:
        if not _type_ok(value, self.kind):
            raise ToolArgumentError(
                f"{where} 期望 {self.kind}，实际 {_type_name(value)}"
            )

        if self.enum is not None and not any(
            _same_value(value, allowed) for allowed in self.enum
        ):
            raise ToolArgumentError(f"{where} 不在允许的取值里：{value!r}")

        if self.kind == "object":
            self._check_object(value, where)
        elif self.kind == "array":
            self._check_array(value, where)
        elif self.kind == "string":
            self._check_string(value, where)
        elif self.kind in ("integer", "number"):
            self._check_number(value, where)

    def _check_object(self, value: dict[str, Any], where: str) -> None:
        props = dict(self.properties)
        missing = [k for k in self.required if k not in value]
        if missing:
            # 顶层保持旧消息格式：既有日志与调用方的阅读习惯不变。
            prefix = "" if where == "参数" else f"{where} "
            raise ToolArgumentError(f"{prefix}缺少必填参数：{missing}")
        if self.additional is False:
            extra = [k for k in value if k not in props]
            if extra:
                prefix = "" if where == "参数" else f"{where} "
                raise ToolArgumentError(f"{prefix}不允许的参数：{extra}")
        for key, item in value.items():
            node = props.get(key)
            if node is not None:
                node.check(item, f"{where}.{key}")

    def _check_array(self, value: list[Any], where: str) -> None:
        if self.min_items is not None and len(value) < self.min_items:
            raise ToolArgumentError(
                f"{where} 至少需要 {self.min_items} 个元素，实际 {len(value)}"
            )
        if self.max_items is not None and len(value) > self.max_items:
            raise ToolArgumentError(
                f"{where} 最多允许 {self.max_items} 个元素，实际 {len(value)}"
            )
        if self.items is not None:
            for index, item in enumerate(value):
                self.items.check(item, f"{where}[{index}]")

    def _check_string(self, value: str, where: str) -> None:
        if self.min_length is not None and len(value) < self.min_length:
            raise ToolArgumentError(
                f"{where} 长度至少 {self.min_length}，实际 {len(value)}"
            )
        if self.max_length is not None and len(value) > self.max_length:
            raise ToolArgumentError(
                f"{where} 长度最多 {self.max_length}，实际 {len(value)}"
            )
        if self.pattern is not None and self.pattern.search(value) is None:
            # 回显 pattern 是安全的：它来自注册表，不是模型输入。
            raise ToolArgumentError(
                f"{where} 不匹配 pattern {self.pattern.pattern!r}"
            )

    def _check_number(self, value: int | float, where: str) -> None:
        if self.minimum is not None and value < self.minimum:
            raise ToolArgumentError(f"{where} 不能小于 {self.minimum}，实际 {value}")
        if self.maximum is not None and value > self.maximum:
            raise ToolArgumentError(f"{where} 不能大于 {self.maximum}，实际 {value}")


def _type_name(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    if value is None:
        return "null"
    return type(value).__name__


def _type_ok(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    raise ToolSchemaError(f"未知的参数类型：{expected!r}")  # pragma: no cover


def _same_value(left: Any, right: Any) -> bool:
    """`enum` 比较：`True` 不等于 `1`（Python 里它们相等，JSON 里不）。"""
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    return left == right


# ---------------------------------------------------------------- 编译


def compile_schema(schema: Any, *, where: str = "") -> CompiledSchema:
    """编译并**拒绝**任何方言外的东西。`where` 用于错误信息里指明是哪个工具。"""
    if not isinstance(schema, dict):
        raise ToolSchemaError(f"{_label(where)}schema 必须是对象，实际是 {_type_name(schema)}")
    root = _compile_node(schema, where=where, path="", depth=0)
    if root.kind != "object":
        raise ToolSchemaError(
            f"{_label(where)}参数 schema 的根类型必须是 object，实际是 {root.kind}"
        )
    digest = canonical_digest(schema)
    return CompiledSchema(schema=schema, digest=digest, _root=root)


def _label(where: str) -> str:
    return f"工具 {where} 的" if where else "参数"


def _compile_node(
    schema: Any, *, where: str, path: str, depth: int
) -> _Node:
    if depth > MAX_DEPTH:
        raise ToolSchemaError(
            f"{_label(where)}{path or 'schema'} 嵌套超过 {MAX_DEPTH} 层，拒绝准入"
        )
    if not isinstance(schema, dict):
        raise ToolSchemaError(
            f"{_label(where)}{path or 'schema'} 必须是对象，实际是 {_type_name(schema)}"
        )

    # 注解先摘掉：它们在哪一节点都合法，且不参与校验。
    body = {k: v for k, v in schema.items() if k not in _ANNOTATIONS}
    for key in _ANNOTATIONS & set(schema):
        if not isinstance(schema[key], str):
            raise ToolSchemaError(
                f"{_label(where)}{_join(path, key)} 必须是字符串"
            )

    if "type" not in body:
        raise ToolSchemaError(
            f"{_label(where)}{path or 'schema'} 缺少 type（本方言不允许隐式类型）"
        )
    kind = body["type"]
    if kind not in _TYPES:
        raise ToolSchemaError(
            f"{_label(where)}{_join(path, 'type')} 不是本方言支持的类型：{kind!r}"
            f"（支持：{sorted(_TYPES)}）"
        )

    allowed = (
        _OBJECT_KEYS if kind == "object"
        else _ARRAY_KEYS if kind == "array"
        else _SCALAR_KEYS
    )
    _reject_unknown(body, allowed, where=where, path=path, kind=kind)

    if kind == "object":
        return _compile_object(body, where=where, path=path, depth=depth)
    if kind == "array":
        return _compile_array(body, where=where, path=path, depth=depth)
    if kind == "string":
        return _compile_string(body, where=where, path=path)
    return _compile_number(body, where=where, path=path, kind=kind)


def _join(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def _reject_unknown(
    body: dict[str, Any], allowed: frozenset[str], *, where: str,
    path: str, kind: str,
) -> None:
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise ToolSchemaError(
            f"{_label(where)}{path or 'schema'} 使用了本方言不支持的约束：{unknown}"
            f"（{kind} 允许：{sorted(allowed)}）。"
            "未实现的关键字必须删掉或先实现——静默忽略会让 schema 变成装饰。"
        )


def _compile_object(
    body: dict[str, Any], *, where: str, path: str, depth: int
) -> _Node:
    raw_props = body.get("properties", {})
    if not isinstance(raw_props, dict):
        raise ToolSchemaError(
            f"{_label(where)}{_join(path, 'properties')} 必须是对象"
        )
    compiled: list[tuple[str, _Node]] = []
    for name, sub in raw_props.items():
        if not isinstance(name, str) or not name:
            raise ToolSchemaError(
                f"{_label(where)}{_join(path, 'properties')} 的属性名必须是非空字符串"
            )
        if name in MODEL_FORBIDDEN_ARGS:
            raise ToolSchemaError(
                f"{_label(where)}{_join(path, 'properties')} 声明了身份字段 {name!r}："
                "执行上下文（task/run/call/attempt、deadline、预算账户）只能由系统提供，"
                "不能作为工具参数"
            )
        compiled.append((
            name,
            _compile_node(
                sub, where=where, path=_join(_join(path, "properties"), name),
                depth=depth + 1,
            ),
        ))

    raw_required = body.get("required", [])
    if not isinstance(raw_required, list) or any(
        not isinstance(item, str) for item in raw_required
    ):
        raise ToolSchemaError(
            f"{_label(where)}{_join(path, 'required')} 必须是字符串数组"
        )
    declared = {name for name, _ in compiled}
    undeclared = [name for name in raw_required if name not in declared]
    if undeclared:
        raise ToolSchemaError(
            f"{_label(where)}{_join(path, 'required')} 引用了未声明的属性：{undeclared}"
        )
    if len(set(raw_required)) != len(raw_required):
        raise ToolSchemaError(
            f"{_label(where)}{_join(path, 'required')} 有重复项：{raw_required}"
        )

    additional: bool | None = None
    if "additionalProperties" in body:
        value = body["additionalProperties"]
        if not isinstance(value, bool):
            raise ToolSchemaError(
                f"{_label(where)}{_join(path, 'additionalProperties')} 只允许 bool："
                "本方言不支持用 schema 描述额外属性（那需要 $ref/patternProperties 语义）"
            )
        additional = value

    return _Node(
        kind="object",
        properties=tuple(compiled),
        required=tuple(raw_required),
        additional=additional,
    )


def _compile_array(
    body: dict[str, Any], *, where: str, path: str, depth: int
) -> _Node:
    if "items" not in body:
        raise ToolSchemaError(
            f"{_label(where)}{path or 'schema'} 是数组但没有 items："
            "本方言要求数组元素类型显式声明"
        )
    items = _compile_node(
        body["items"], where=where, path=_join(path, "items"), depth=depth + 1
    )
    min_items = _optional_int(body, "minItems", where=where, path=path, floor=0)
    max_items = _optional_int(body, "maxItems", where=where, path=path, floor=0)
    if (
        min_items is not None and max_items is not None and min_items > max_items
    ):
        raise ToolSchemaError(
            f"{_label(where)}{path or 'schema'} 的 minItems 大于 maxItems"
        )
    return _Node(
        kind="array", items=items, min_items=min_items, max_items=max_items
    )


def _compile_string(body: dict[str, Any], *, where: str, path: str) -> _Node:
    min_length = _optional_int(body, "minLength", where=where, path=path, floor=0)
    max_length = _optional_int(body, "maxLength", where=where, path=path, floor=0)
    if (
        min_length is not None and max_length is not None
        and min_length > max_length
    ):
        raise ToolSchemaError(
            f"{_label(where)}{path or 'schema'} 的 minLength 大于 maxLength"
        )
    pattern = None
    if "pattern" in body:
        raw = body["pattern"]
        if not isinstance(raw, str):
            raise ToolSchemaError(
                f"{_label(where)}{_join(path, 'pattern')} 必须是字符串"
            )
        try:
            pattern = re.compile(raw)
        except re.error as exc:
            raise ToolSchemaError(
                f"{_label(where)}{_join(path, 'pattern')} 不是合法正则：{exc}"
            ) from exc
    enum = _compile_enum(body, where=where, path=path, kind="string")
    return _Node(
        kind="string",
        enum=enum,
        min_length=min_length,
        max_length=max_length,
        pattern=pattern,
    )


def _compile_number(
    body: dict[str, Any], *, where: str, path: str, kind: str
) -> _Node:
    minimum = _optional_int(body, "minimum", where=where, path=path, floor=None)
    maximum = _optional_int(body, "maximum", where=where, path=path, floor=None)
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ToolSchemaError(
            f"{_label(where)}{path or 'schema'} 的 minimum 大于 maximum"
        )
    enum = _compile_enum(body, where=where, path=path, kind=kind)
    return _Node(
        kind=kind, enum=enum, minimum=minimum, maximum=maximum
    )


def _compile_enum(
    body: dict[str, Any], *, where: str, path: str, kind: str
) -> tuple[Any, ...] | None:
    if "enum" not in body:
        return None
    raw = body["enum"]
    if not isinstance(raw, list) or not raw:
        raise ToolSchemaError(
            f"{_label(where)}{_join(path, 'enum')} 必须是非空数组"
        )
    for item in raw:
        if not _type_ok(item, kind):
            raise ToolSchemaError(
                f"{_label(where)}{_join(path, 'enum')} 里的 {item!r} 不是 {kind}"
            )
    return tuple(raw)


def _optional_int(
    body: dict[str, Any], key: str, *, where: str, path: str, floor: int | None
) -> int | None:
    if key not in body:
        return None
    value = body[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise ToolSchemaError(
            f"{_label(where)}{_join(path, key)} 必须是整数"
            "（本方言不接受浮点：摘要与比较只按整数定义）"
        )
    if floor is not None and value < floor:
        raise ToolSchemaError(
            f"{_label(where)}{_join(path, key)} 不能小于 {floor}"
        )
    return value


def compile_cached(schema: dict[str, Any], *, where: str = "") -> CompiledSchema:
    """带缓存的编译（执行期热路径用）。同一份 schema 只编译一次。"""
    key = canonical_digest(schema) + "\x00" + where
    compiled = _CACHE.get(key)
    if compiled is None:
        compiled = compile_schema(schema, where=where)
        _CACHE[key] = compiled
    return compiled


def clear_cache() -> None:
    """测试用：注册表被打补丁后清掉缓存，避免拿旧编译结果说话。"""
    _CACHE.clear()


__all__ = [
    "CompiledSchema",
    "MODEL_FORBIDDEN_ARGS",
    "MAX_DEPTH",
    "clear_cache",
    "compile_cached",
    "compile_schema",
]
