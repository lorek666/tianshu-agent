"""首日配置、凭据状态、定时任务、账户预算与 MCP 状态的控制面实现。

为什么单独成模块（与 `memory_control.py` 同构）：
1. 避免继续膨胀 `control.py`；
2. 严守凭据与授权红线：
   - **密钥绝不落 `model_config.json`、不进事件日志、不在 RPC 响应或错误消息中回显**
     （连前后缀都不回显，只返回布尔 `configured`）；
   - **不静默降级**：平台不支持 Keychain 或当前为只读 `env` 后端时，写入/删除明确返回
     `unsupported`，绝不退回明文文件；
   - **测试完全隔离**：`RuntimeModelSlot` 接受注入 `stores`，单测不碰宿主机钥匙串。
"""

from __future__ import annotations

import json
import os
import sys
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .budget import DIMENSIONS
from .capability import CapabilityProfile
from .credentials import (
    ENV_VARS,
    CredentialRef,
    CredentialStore,
    EnvCredentialStore,
    KeychainCredentialStore,
    redact,
    register_secret,
)
from .errors import (
    CredentialUnsupported,
    CredentialWriteRefused,
    TianshuError,
    WorkspaceViolation,
)
from .execpolicy import ExecPolicy
from .loop import Stack
from .model import (
    AnthropicMessagesModel,
    ModelIdentity,
    ModelResponse,
    ModelStreamDelta,
    OpenAICompatibleModel,
    ToolCall,
    identity_of,
)
from .scheduler import Scheduler

VALID_MODES = ("scripted", "openai", "anthropic")
VALID_BACKENDS = ("keychain", "env")
KNOWN_PROVIDERS = ("openai", "anthropic")
ALLOWED_MODEL_CONFIG_KEYS = frozenset(
    {"mode", "model_name", "base_url", "credential_backend"}
)
MODEL_CONFIG_FILE = "model_config.json"


def _default_backend_name() -> str:
    env_choice = (os.environ.get("TIANSHU_CREDENTIAL_BACKEND") or "").strip().lower()
    if env_choice in VALID_BACKENDS:
        return env_choice
    return "keychain" if sys.platform == "darwin" else "env"


def _is_official_host(base_url: str, official_host: str) -> bool:
    if not official_host:
        return False
    try:
        parsed = urlsplit(base_url)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and (parsed.hostname or "").lower() == official_host.lower()
    )


class _StatelessScriptedModel:
    """无状态、线程安全、可重复运行的离线演示脚本模型。

    热切回 `mode="scripted"` 时不能每次新建固定序列 `ScriptedModel`（否则每轮都会
    重复弹出第 0 条工具调用导致死循环），也不能共享可变列表（跑完一次即耗尽且非线程安全）。
    这里纯按输入 `messages` 的历史轮次决定动作：首轮若存在 `list_files` 工具则列目录，
    次轮收敛 `end_turn`。
    """

    def capability_profile(self) -> CapabilityProfile:
        return CapabilityProfile(
            provider="scripted",
            model="scripted",
            supports_tool_calling=True,
            supports_parallel_tool_calls=True,
            supports_streaming=True,
            declared_by="ScriptedModel（脚本替身）",
        )

    def price_identity(self) -> ModelIdentity:
        return ModelIdentity(adapter="scripted", vendor=None, model="")

    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelResponse:
        assistant_turns = sum(1 for m in messages if m.get("role") == "assistant")
        has_list_files = any(
            isinstance(t, dict) and t.get("name") == "list_files" for t in tools
        )
        if assistant_turns == 0 and has_list_files:
            return ModelResponse(
                finish_reason="tool_use",
                tool_calls=[ToolCall("c_script_1", "list_files", {"path": "."})],
                usage={"input_tokens": 16, "output_tokens": 8},
            )
        return ModelResponse(
            finish_reason="end_turn",
            content="已完成目录检查（演示脚本模型）。",
            usage={"input_tokens": 24, "output_tokens": 12},
        )

    def stream_complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_delta: Any,
    ) -> ModelResponse:
        resp = self.complete(messages, tools)
        for tc in resp.tool_calls:
            on_delta(ModelStreamDelta(kind="tool_call_start", tool_name=tc.name))
        if resp.content:
            mid = max(1, len(resp.content) // 2)
            for part in (resp.content[:mid], resp.content[mid:]):
                if part:
                    on_delta(ModelStreamDelta(kind="text", text=part))
        return resp


_STATELESS_SCRIPTED = _StatelessScriptedModel()


def _validate_base_url(raw: str) -> str:
    """校验端点 URL；绝不在异常消息中回显原始输入，防止误粘入的密钥被原样反射。"""
    url = raw.strip()
    if not url:
        return ""
    if len(url) >= 8 and "://" not in url:
        register_secret(url)
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise ValueError("base_url 格式非法：必须是合法的 http(s) URL") from exc
    if parsed.username is not None or parsed.password is not None:
        if parsed.password:
            register_secret(parsed.password)
        raise ValueError(
            "base_url 不得包含用户名或密码（凭据请走系统钥匙串或环境变量）"
        )
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("base_url 必须是合法的 http 或 https URL（不含密钥）")
    return url.rstrip("/")


@dataclass(frozen=True)
class ModelConfig:
    """非敏感模型路由配置（落盘 `<data_root>/model_config.json`，不含任何密钥）。"""

    mode: str = "scripted"
    model_name: str = ""
    base_url: str = ""
    credential_backend: str = "env"

    @classmethod
    def of(cls, raw: Mapping[str, Any]) -> ModelConfig:
        if not isinstance(raw, Mapping):
            raise ValueError("模型配置必须是对象")
        unknown_keys = set(raw.keys()) - ALLOWED_MODEL_CONFIG_KEYS
        if unknown_keys:
            for key in unknown_keys:
                val = raw.get(key)
                if isinstance(val, str) and len(val.strip()) >= 8:
                    register_secret(val.strip())
            sorted_names = ", ".join(sorted(str(k) for k in unknown_keys))
            raise ValueError(
                f"model.configure 拒绝未知或敏感字段（{redact(sorted_names)}）；"
                "密钥请通过 credential.set 或环境变量提供"
            )
        mode = str(raw.get("mode") or "scripted").strip().lower()
        if mode not in VALID_MODES:
            raise ValueError(f"未知模型模式（可选：{list(VALID_MODES)}）")
        model_name = str(raw.get("model_name") or "").strip()
        if mode == "anthropic" and not model_name:
            raise ValueError(
                "anthropic 模式必须显式填写 model_name（例如 claude-sonnet-4-5）"
            )
        if mode == "openai" and not model_name:
            model_name = "gpt-4o-mini"
        if mode == "scripted":
            model_name = ""
        raw_base_url = raw.get("base_url")
        if raw_base_url is not None and not isinstance(raw_base_url, str):
            raise ValueError("base_url 必须是字符串")
        base_url = _validate_base_url(raw_base_url or "")
        backend = str(
            raw.get("credential_backend") or _default_backend_name()
        ).strip().lower()
        if backend not in VALID_BACKENDS:
            raise ValueError(f"未知凭据后端（可选：{list(VALID_BACKENDS)}）")
        return cls(
            mode=mode,
            model_name=model_name,
            base_url=base_url,
            credential_backend=backend,
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "mode": self.mode,
            "model_name": self.model_name,
            "base_url": self.base_url,
            "credential_backend": self.credential_backend,
        }


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    from .fsutil import atomic_write_bytes

    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode(
        "utf-8"
    )
    atomic_write_bytes(path, encoded)
    if sys.platform != "win32":
        os.chmod(path, 0o600)


class RuntimeModelSlot:
    """线程安全的运行时模型槽位：支持首日 GUI 热切换模型，同时保持既有传入模型语义不变。"""

    def __init__(
        self,
        fallback_model: Any,
        *,
        data_root: Path | None = None,
        stores: Mapping[str, CredentialStore] | None = None,
    ) -> None:
        self._fallback = fallback_model
        self._data_root = Path(data_root) if data_root is not None else None
        self._lock = threading.Lock()
        self._stores: dict[str, CredentialStore] = (
            dict(stores)
            if stores is not None
            else {
                "env": EnvCredentialStore(),
                "keychain": KeychainCredentialStore(),
            }
        )
        self._config: ModelConfig | None = self._load_saved_config()
        self._revision: int = 0

    @property
    def config_revision(self) -> int:
        with self._lock:
            return self._revision

    def _config_path(self) -> Path | None:
        if self._data_root is None:
            return None
        return self._data_root / MODEL_CONFIG_FILE

    def _load_saved_config(self) -> ModelConfig | None:
        path = self._config_path()
        if path is None or not path.is_file():
            return None
        raw = json.loads(path.read_bytes().decode("utf-8"))
        if not isinstance(raw, dict):
            raise TianshuError(f"模型配置文件损坏（非 JSON 对象）：{path}")
        return ModelConfig.of(raw)

    def store_for(self, backend: str) -> CredentialStore:
        store = self._stores.get(backend)
        if store is None:
            raise CredentialUnsupported(
                f"未知凭据后端（可选：{sorted(self._stores)}）"
            )
        return store

    def adapter_name(self) -> str:
        with self._lock:
            cfg = self._config
        if cfg is None:
            return type(self._fallback).__name__
        if cfg.mode == "anthropic":
            return AnthropicMessagesModel.__name__
        if cfg.mode == "openai":
            return OpenAICompatibleModel.__name__
        return "ScriptedModel"

    def current_config(self) -> ModelConfig:
        with self._lock:
            if self._config is not None:
                return self._config
        ident = identity_of(self._fallback)
        if ident is not None and ident.adapter == "anthropic":
            return ModelConfig(
                mode="anthropic",
                model_name=ident.model,
                base_url=getattr(self._fallback, "base_url", ""),
                credential_backend=_default_backend_name(),
            )
        if ident is not None and ident.adapter == "openai-compatible":
            return ModelConfig(
                mode="openai",
                model_name=ident.model,
                base_url=getattr(self._fallback, "base_url", ""),
                credential_backend=_default_backend_name(),
            )
        return ModelConfig(
            mode="scripted",
            model_name="",
            base_url="",
            credential_backend=_default_backend_name(),
        )

    def configure(self, raw: Mapping[str, Any]) -> ModelConfig:
        cfg = ModelConfig.of(raw)
        if cfg.mode in KNOWN_PROVIDERS:
            store = self.store_for(cfg.credential_backend)
            if not store.capabilities.implemented:
                # 提前在配置时拦截当前平台未实现的后端（直接调 get 触发诚实原因）
                store.get(CredentialRef(cfg.mode, "llm"))
        path = self._config_path()
        with self._lock:
            if path is not None:
                _atomic_write_json(path, cfg.to_dict())
            self._config = cfg
            self._revision += 1
        return cfg

    def _resolve_active_model(self) -> Any:
        with self._lock:
            cfg = self._config
        if cfg is None:
            return self._fallback
        if cfg.mode == "scripted":
            return _STATELESS_SCRIPTED
        store = self.store_for(cfg.credential_backend)
        secret = store.get(CredentialRef(cfg.mode, "llm"))
        api_key = "" if secret is None else str(secret)
        if cfg.mode == "anthropic":
            return AnthropicMessagesModel(
                cfg.model_name,
                base_url=cfg.base_url or None,
                api_key=api_key,
            )
        return OpenAICompatibleModel(
            cfg.model_name or "gpt-4o-mini",
            base_url=cfg.base_url or None,
            api_key=api_key,
        )

    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Any:
        return self._resolve_active_model().complete(messages, tools)

    def stream_complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_delta: Any,
    ) -> Any:
        active = self._resolve_active_model()
        streamer = getattr(active, "stream_complete", None)
        if callable(streamer):
            return streamer(messages, tools, on_delta)
        return active.complete(messages, tools)

    def capability_profile(self) -> Any:
        """准入阶段只读取适配器静态能力，不要求此时 API Key 已就绪（避免吞掉 auth 错误）。"""
        with self._lock:
            cfg = self._config
        if cfg is None:
            getter = getattr(self._fallback, "capability_profile", None)
            return getter() if callable(getter) else None
        if cfg.mode == "scripted":
            return _STATELESS_SCRIPTED.capability_profile()
        if cfg.mode == "anthropic":
            return CapabilityProfile(
                provider=AnthropicMessagesModel.ADAPTER,
                model=cfg.model_name,
                supports_tool_calling=AnthropicMessagesModel.SUPPORTS_TOOL_CALLING,
                supports_parallel_tool_calls=AnthropicMessagesModel.SUPPORTS_PARALLEL_TOOL_CALLS,
                supports_streaming=AnthropicMessagesModel.SUPPORTS_STREAMING,
                supports_thinking=AnthropicMessagesModel.SUPPORTS_THINKING,
                context_window_bytes=AnthropicMessagesModel.CONTEXT_WINDOW_BYTES,
                output_tokens=4096,
                declared_by=f"{AnthropicMessagesModel.__name__}（adapter 声明）",
            )
        return CapabilityProfile(
            provider=OpenAICompatibleModel.ADAPTER,
            model=cfg.model_name or "gpt-4o-mini",
            supports_tool_calling=OpenAICompatibleModel.SUPPORTS_TOOL_CALLING,
            supports_parallel_tool_calls=OpenAICompatibleModel.SUPPORTS_PARALLEL_TOOL_CALLS,
            supports_streaming=OpenAICompatibleModel.SUPPORTS_STREAMING,
            supports_thinking=OpenAICompatibleModel.SUPPORTS_THINKING,
            context_window_bytes=OpenAICompatibleModel.CONTEXT_WINDOW_BYTES,
            output_tokens=None,
            declared_by=f"{OpenAICompatibleModel.__name__}（adapter 声明）",
        )

    def price_identity(self) -> ModelIdentity | None:
        """计价身份只依赖协议、模型名与端点主机，不依赖密钥是否已写入。"""
        with self._lock:
            cfg = self._config
        if cfg is None:
            return identity_of(self._fallback)
        if cfg.mode == "scripted":
            return _STATELESS_SCRIPTED.price_identity()
        if cfg.mode == "anthropic":
            eff_url = (
                cfg.base_url
                or os.environ.get("ANTHROPIC_BASE_URL")
                or "https://api.anthropic.com/v1"
            )
            official = _is_official_host(eff_url, AnthropicMessagesModel.OFFICIAL_HOST)
            return ModelIdentity(
                adapter=AnthropicMessagesModel.ADAPTER,
                vendor=AnthropicMessagesModel.VENDOR if official else None,
                model=cfg.model_name,
            )
        eff_url = (
            cfg.base_url
            or os.environ.get("TIANSHU_BASE_URL")
            or "https://api.openai.com/v1"
        )
        official = _is_official_host(eff_url, OpenAICompatibleModel.OFFICIAL_HOST)
        return ModelIdentity(
            adapter=OpenAICompatibleModel.ADAPTER,
            vendor=OpenAICompatibleModel.VENDOR if official else None,
            model=cfg.model_name or "gpt-4o-mini",
        )


def credential_views(slot: RuntimeModelSlot) -> list[dict[str, Any]]:
    """返回各 provider 在 keychain 与 env 下的就绪状态；绝不回显任何密钥内容。"""
    cfg = slot.current_config()
    items: list[dict[str, Any]] = []
    for backend in VALID_BACKENDS:
        store = slot.store_for(backend)
        caps = store.capabilities
        for provider in KNOWN_PROVIDERS:
            ref = CredentialRef(provider, "llm")
            configured = False
            error: str | None = None
            if caps.implemented:
                try:
                    configured = store.get(ref) is not None
                except CredentialUnsupported as exc:
                    error = redact(str(exc))
            else:
                error = "当前平台尚未实现该安全存储后端"
            items.append(
                {
                    "provider": provider,
                    "capability_class": "llm",
                    "backend": backend,
                    "active_backend": backend == cfg.credential_backend,
                    "implemented": bool(caps.implemented),
                    "secure_storage": bool(caps.secure_storage),
                    "writable": bool(caps.supports_write),
                    "configured": configured,
                    "env_var": (
                        ENV_VARS.get((provider, "llm")) if backend == "env" else None
                    ),
                    "error": error,
                }
            )
    return items


def settings_view(slot: RuntimeModelSlot) -> dict[str, Any]:
    cfg = slot.current_config()
    summary = (
        "演示脚本模型（离线）"
        if cfg.mode == "scripted"
        else f"{cfg.mode} · {cfg.model_name}"
    )
    return {
        "model": {
            **cfg.to_dict(),
            "summary": summary,
        },
        "credentials": credential_views(slot),
    }


def _validate_credential_target(
    params: Mapping[str, Any], error_cls: type[Exception]
) -> tuple[CredentialRef, str]:
    raw_provider = params.get("provider")
    if isinstance(raw_provider, str) and len(raw_provider.strip()) >= 8:
        register_secret(raw_provider.strip())
    provider = str(raw_provider or "").strip().lower()
    if provider not in KNOWN_PROVIDERS:
        raise error_cls(
            "bad_params", f"provider 仅支持 {list(KNOWN_PROVIDERS)}"
        )
    capability_class = str(params.get("capability_class") or "llm").strip().lower()
    if capability_class != "llm":
        raise error_cls("bad_params", "capability_class 当前仅支持 'llm'")
    backend = str(params.get("backend") or "keychain").strip().lower()
    if backend not in VALID_BACKENDS:
        raise error_cls("bad_params", f"未知凭据后端（可选：{list(VALID_BACKENDS)}）")
    return CredentialRef(provider, capability_class), backend


def credential_set(
    slot: RuntimeModelSlot, params: Mapping[str, Any], error_cls: type[Exception]
) -> dict[str, Any]:
    raw_secret = params.get("secret")
    if isinstance(raw_secret, str) and raw_secret.strip():
        register_secret(raw_secret.strip())
    if not isinstance(raw_secret, str) or not raw_secret.strip():
        raise error_cls("bad_params", "secret 必须是非空字符串")
    secret = raw_secret.strip()
    register_secret(secret)

    ref, backend = _validate_credential_target(params, error_cls)
    store = slot.store_for(backend)
    try:
        store.set(ref, secret)
    except (CredentialUnsupported, CredentialWriteRefused) as exc:
        raise error_cls("unsupported", redact(str(exc))) from exc
    return {"credentials": credential_views(slot)}


def credential_delete(
    slot: RuntimeModelSlot, params: Mapping[str, Any], error_cls: type[Exception]
) -> dict[str, Any]:
    ref, backend = _validate_credential_target(params, error_cls)
    store = slot.store_for(backend)
    try:
        store.delete(ref)
    except (CredentialUnsupported, CredentialWriteRefused) as exc:
        raise error_cls("unsupported", redact(str(exc))) from exc
    return {"credentials": credential_views(slot)}


def schedule_list(stack: Stack) -> dict[str, Any]:
    scheduler = Scheduler(stack.log)
    return {"schedules": scheduler.list()}


def schedule_create(
    stack: Stack, params: Mapping[str, Any], error_cls: type[Exception]
) -> dict[str, Any]:
    goal = params.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        raise error_cls("bad_params", "goal 必须是非空字符串")
    interval = params.get("interval_seconds")
    if not isinstance(interval, int) or isinstance(interval, bool) or interval < 1:
        raise error_cls("bad_params", "interval_seconds 必须是 >= 1 的整数")
    raw_ws = params.get("workspaces")
    if raw_ws is None:
        workspaces: list[Any] = []
    elif isinstance(raw_ws, list):
        workspaces = raw_ws
    else:
        raise error_cls("bad_params", "workspaces 必须是列表")
    raw_policy = params.get("exec_policy")
    exec_policy = (
        ExecPolicy.from_dict(raw_policy) if isinstance(raw_policy, dict) else None
    )
    scheduler = Scheduler(stack.log)
    try:
        created = scheduler.create(
            goal, workspaces, interval_seconds=interval, exec_policy=exec_policy
        )
    except (WorkspaceViolation, TianshuError) as exc:
        raise error_cls("bad_params", redact(str(exc))) from exc
    return {"schedule": created}


def schedule_set_enabled(
    stack: Stack, params: Mapping[str, Any], error_cls: type[Exception]
) -> dict[str, Any]:
    schedule_id = params.get("schedule_id")
    if not isinstance(schedule_id, str) or not schedule_id.strip():
        raise error_cls("bad_params", "schedule_id 必须是非空字符串")
    enabled = params.get("enabled")
    if not isinstance(enabled, bool):
        raise error_cls("bad_params", "enabled 必须是布尔值")
    scheduler = Scheduler(stack.log)
    try:
        updated = scheduler.set_enabled(schedule_id.strip(), enabled)
    except TianshuError as exc:
        raise error_cls("not_found", redact(str(exc))) from exc
    return {"schedule": updated}


def schedule_remove(
    stack: Stack, params: Mapping[str, Any], error_cls: type[Exception]
) -> dict[str, Any]:
    schedule_id = params.get("schedule_id")
    if not isinstance(schedule_id, str) or not schedule_id.strip():
        raise error_cls("bad_params", "schedule_id 必须是非空字符串")
    scheduler = Scheduler(stack.log)
    try:
        scheduler.remove(schedule_id.strip())
    except TianshuError as exc:
        raise error_cls("not_found", redact(str(exc))) from exc
    return {"removed": schedule_id.strip()}


def budget_view(
    stack: Stack, params: Mapping[str, Any], error_cls: type[Exception]
) -> dict[str, Any]:
    ledger = getattr(stack, "budget", None)
    if ledger is None:
        return {
            "dimensions": list(DIMENSIONS),
            "run_account": None,
            "accounts": [],
            "unsettled_count": 0,
        }
    run_id = params.get("run_id")
    if run_id is not None and (not isinstance(run_id, str) or not run_id.strip()):
        raise error_cls("bad_params", "run_id 必须是非空字符串")
    run_account: dict[str, Any] | None = None
    if isinstance(run_id, str) and run_id.strip():
        acc_id = f"run:{run_id.strip()}"
        if ledger.account(acc_id) is not None:
            run_account = {
                "exists": True,
                **ledger.usage(acc_id),
                "unsettled": ledger.reconcile(acc_id),
            }
        else:
            run_account = {
                "account_id": acc_id,
                "exists": False,
                "limits": {},
                "settled": {},
                "reserved": {},
                "unknown_reservations": 0,
                "cancelled": False,
                "overrun": False,
                "bound": "",
                "unsettled": [],
            }
    rows = stack.log.conn.execute(
        "SELECT account_id FROM budget_accounts"
        " WHERE parent_id IS NULL ORDER BY created_at DESC LIMIT 20"
    ).fetchall()
    accounts = [ledger.usage(row["account_id"]) for row in rows]
    unsettled_all = ledger.reconcile(None)
    return {
        "dimensions": list(DIMENSIONS),
        "run_account": run_account,
        "accounts": accounts,
        "unsettled_count": len(unsettled_all),
    }


def mcp_list_view(configs: Mapping[str, Any] | None = None) -> dict[str, Any]:
    from .mcp import McpClients, config_from_env
    from .tool_registry import build_registry

    resolved = config_from_env() if configs is None else dict(configs)
    if not resolved:
        return {
            "configured": False,
            "reason": "尚未配置 MCP server（可通过环境变量 TIANSHU_MCP_CONFIG 指定 JSON 配置文件）",
            "servers": [],
            "tools": [],
            "rejected_tools": [],
        }
    clients = McpClients(resolved)
    try:
        clients.start_all()
        registry = build_registry(clients)
        servers = [
            {
                "name": state.name,
                "status": state.status,
                "protocol_version": state.protocol_version,
                "tools": [tool.name for tool in state.tools],
                "tools_truncated": state.tools_truncated,
                "diagnostics": list(state.diagnostics),
                "pid": state.pid,
            }
            for state in clients.states()
        ]
        tools = []
        for tool_id in registry.ids():
            binding = registry.mcp_binding(tool_id)
            if binding is None:
                continue
            tools.append(
                {
                    "tool_id": tool_id,
                    "uri": binding.uri,
                    "server": binding.server,
                    "tool": binding.tool,
                    "tool_version": binding.tool_version,
                    "schema_hash": binding.schema_hash,
                    "narrowed": list(binding.narrowed),
                }
            )
        return {
            "configured": True,
            "reason": None,
            "servers": servers,
            "tools": tools,
            "rejected_tools": registry.rejected_tools(),
        }
    finally:
        clients.close_all()


def diagnostics_preview(
    stack: Stack, params: Mapping[str, Any], error_cls: type[Exception]
) -> dict[str, Any]:
    from .diagnostics_export import preview_diagnostics

    root = getattr(stack, "data_root", None) or stack.log.db_path.parent
    raw_secs = params.get("sections")
    if raw_secs is not None and (
        not isinstance(raw_secs, list) or not all(isinstance(s, str) for s in raw_secs)
    ):
        raise error_cls("bad_params", "sections 必须是字符串列表")
    redact_flag = params.get("redact_sensitive", True)
    if not isinstance(redact_flag, bool):
        raise error_cls("bad_params", "redact_sensitive 必须是布尔值")
    if not redact_flag:
        raise error_cls(
            "unsupported",
            "控制面仅支持默认脱敏；如需本机未脱敏排查包请在本地终端执行 CLI --no-redact",
        )
    try:
        manifest = preview_diagnostics(root, sections=raw_secs, redact_sensitive=True)
    except TianshuError as exc:
        raise error_cls("bad_params", redact(str(exc))) from exc
    return {"manifest": manifest}


def diagnostics_export(
    stack: Stack, params: Mapping[str, Any], error_cls: type[Exception]
) -> dict[str, Any]:
    from .diagnostics_export import export_diagnostics

    out_dir = params.get("out_dir")
    if not isinstance(out_dir, str) or not out_dir.strip():
        raise error_cls("bad_params", "out_dir 必须是非空目录路径字符串")
    root = getattr(stack, "data_root", None) or stack.log.db_path.parent
    raw_secs = params.get("sections")
    if raw_secs is not None and (
        not isinstance(raw_secs, list) or not all(isinstance(s, str) for s in raw_secs)
    ):
        raise error_cls("bad_params", "sections 必须是字符串列表")
    redact_flag = params.get("redact_sensitive", True)
    if not isinstance(redact_flag, bool):
        raise error_cls("bad_params", "redact_sensitive 必须是布尔值")
    if not redact_flag:
        raise error_cls(
            "unsupported",
            "控制面仅支持默认脱敏导出；如需本机未脱敏排查包请在本地终端显式运行 CLI --no-redact",
        )
    try:
        return export_diagnostics(
            root,
            Path(out_dir.strip()),
            sections=raw_secs,
            redact_sensitive=True,
        )
    except TianshuError as exc:
        raise error_cls("bad_params", redact(str(exc))) from exc
