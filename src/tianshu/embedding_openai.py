"""OpenAI 兼容 embedding 端点适配器（W6，SPEC-N2-EMBEDDING-PROVIDER）。

## 外发边界（本模块存在的前提，先于代码说清）

这个 provider 会把传入文本**原样**发给配置的端点，所以：

- `sends_data_off_device = True` 是能力自报；检索 gate
  （`MemoryProjection.retrieve(..., allow_embedding_outbound=False)`，默认
  False）按这个属性判定，**模型工具的检索永不外发**——网络调用只允许发生在
  人显式发起的控制面动作里（`rebuild-vectors` / CLI search / 面板 RPC）。
- 配置**只从环境变量读**（`from_env()`）；API 密钥永不落盘、不进事件日志。
- 缺任一项在装配时报错并点名缺什么，不静默降级成「没有 provider」。

## 与 net.fetch 的关系

刻意**不走** `net.fetch`：那是工具层 GET 执行器（canonical URL + 逐次审批表），
这里是库层「用户显式配置端点 + 人显式命令触发」的批处理调用，两者授权语义
不同。这里的行为：`http.client` 直连、TLS 校验恒开、不跟随重定向（端点是
用户显式指定的 origin，302 换目标等于换端点）、整请求超时、响应按字节上限
读取、失败分类成封闭集。不自动重试：rebuild 是离线显式动作，失败如实报错
由人重跑；`rebuild()` 的事务边界保证半途失败不留下半套新向量。
"""

from __future__ import annotations

import http.client
import json
import os
import ssl
import urllib.parse
from collections.abc import Sequence

from .errors import TianshuError

__all__ = [
    "BATCH_LIMIT",
    "ENV_BASE_URL",
    "ENV_API_KEY",
    "ENV_DIM",
    "ENV_MODEL",
    "ENV_PROVIDER",
    "ENV_TIMEOUT_S",
    "EmbeddingError",
    "OpenAICompatibleEmbeddingProvider",
    "from_env",
]

# 环境变量名的公共前缀。下面定义的全部是变量「名字」，不是任何凭据值；
# 密钥本身只从环境变量读，永不写进源码、配置文件或事件日志。
_ENV_PREFIX = "TIANSHU_EMBEDDING_"
ENV_PROVIDER = _ENV_PREFIX + "PROVIDER"
ENV_BASE_URL = _ENV_PREFIX + "BASE_URL"
ENV_MODEL = _ENV_PREFIX + "MODEL"
ENV_DIM = _ENV_PREFIX + "DIM"
ENV_API_KEY = _ENV_PREFIX + "API_KEY"
ENV_TIMEOUT_S = _ENV_PREFIX + "TIMEOUT_S"

# 单次请求的输入条数上限（OpenAI 兼容端点的常见批限；超了自动分片）。
BATCH_LIMIT = 64

_DEFAULT_TIMEOUT_S = 60.0
# 响应字节上限：64 条 × 3072 维浮点的 JSON 远小于这个数；超出按 shape 拒。
_MAX_RESPONSE_BYTES = 64 * 1024 * 1024
# 报错里引用端点响应体时的截断长度（错误信息只进终端/stderr，不进事件）。
_MAX_ERROR_BODY = 500

# 失败分类封闭集。未知分类在构造时即报错，不兜底。
_REASONS = frozenset({"auth", "quota", "status", "network", "timeout", "shape"})


class EmbeddingError(TianshuError):
    """端点调用失败的封闭集分类（reason 供 CLI 如实转述）。"""

    def __init__(self, reason: str, message: str) -> None:
        if reason not in _REASONS:
            raise ValueError(f"未知的 embedding 失败分类：{reason!r}")
        super().__init__(f"[{reason}] {message}")
        self.reason = reason


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _parse_base_url(base_url: str) -> tuple[str, str, int, str]:
    """解析端点前缀，返回 (scheme, host, port, path)。

    接受 https://host[:port]/v1 这类前缀；拒绝 userinfo / query / fragment
    与空 host。路径保留（/v1 拼成 /v1/embeddings），结尾斜杠折叠。
    """
    parts = urllib.parse.urlsplit(base_url)
    if parts.scheme not in ("http", "https"):
        raise TianshuError(f"embedding 端点必须是 http/https URL：{base_url!r}")
    if not parts.hostname:
        raise TianshuError(f"embedding 端点缺 host：{base_url!r}")
    if parts.username or parts.password:
        raise TianshuError(
            "embedding 端点不接受 userinfo（凭据只走 TIANSHU_EMBEDDING_API_KEY 环境变量）"
        )
    if parts.query or parts.fragment:
        raise TianshuError("embedding 端点不接受 query / fragment")
    port = parts.port
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    return parts.scheme, parts.hostname, port, parts.path.rstrip("/")


def _classify_status(status: int, body_digest: str) -> EmbeddingError:
    if status in (401, 403):
        return EmbeddingError("auth", f"端点拒绝凭据（HTTP {status}）{body_digest}")
    if status in (402, 429):
        return EmbeddingError("quota", f"端点配额/限流（HTTP {status}）{body_digest}")
    return EmbeddingError("status", f"端点返回 HTTP {status}{body_digest}")


class OpenAICompatibleEmbeddingProvider:
    """POST <base_url>/embeddings 的最小适配器。

    model_id 带 provider 前缀（openai:<model>）——它是索引空间身份：
    换模型即换空间，旧向量不参与融合直到 rebuild_vectors() 重算
    （memory_index 的既有机制）。
    """

    # 能力自报：这个 provider 会把传入文本发到网络（gate 依据，见模块 docstring）。
    sends_data_off_device = True

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        dim: int,
        api_key: str,
        timeout_s: float | None = None,
    ) -> None:
        scheme, host, port, path = _parse_base_url(base_url)
        if not isinstance(model, str) or not model.strip():
            raise TianshuError("embedding 模型名必须是非空字符串")
        if isinstance(dim, bool) or not isinstance(dim, int) or dim <= 0:
            raise TianshuError(f"embedding 维度必须是正整数：{dim!r}")
        if not isinstance(api_key, str) or not api_key.strip():
            raise TianshuError("embedding API 密钥必须是非空字符串")
        self._scheme = scheme
        self._host = host
        self._port = port
        self._path = f"{path}/embeddings" if path else "/embeddings"
        self._model = model.strip()
        self._api_key = api_key
        self._timeout_s = (
            float(timeout_s) if timeout_s is not None
            else _env_float(ENV_TIMEOUT_S, _DEFAULT_TIMEOUT_S)
        )
        if self._timeout_s <= 0:
            raise TianshuError(f"embedding 超时必须是正数：{self._timeout_s!r}")
        self.model_id = f"openai:{self._model}"
        self.dim = dim

    # ---- 协议面 ----

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """批量嵌入；内部按 BATCH_LIMIT 分片逐批请求，失败即停。"""
        vectors: list[list[float]] = []
        for start in range(0, len(texts), BATCH_LIMIT):
            batch = list(texts[start : start + BATCH_LIMIT])
            vectors.extend(self._embed_batch(batch))
        return vectors

    # ---- HTTP ----

    def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        body = json.dumps(
            {"model": self._model, "input": batch}, ensure_ascii=False
        ).encode("utf-8")
        status, raw = self._request(body)
        if status != 200:
            raise _classify_status(status, self._digest(raw))
        return self._parse_vectors(raw, expected=len(batch))

    def _request(self, body: bytes) -> tuple[int, bytes]:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
            "User-Agent": "tianshu-agent/1 embedding",
            "Accept": "application/json",
            "Connection": "close",
        }
        try:
            if self._scheme == "https":
                conn: http.client.HTTPConnection
                conn = http.client.HTTPSConnection(
                    self._host,
                    self._port,
                    timeout=self._timeout_s,
                    context=ssl.create_default_context(),
                )
            else:
                conn = http.client.HTTPConnection(
                    self._host, self._port, timeout=self._timeout_s,
                )
        except OSError as exc:
            raise EmbeddingError("network", f"端点连接失败：{exc}") from exc
        try:
            conn.request("POST", self._path, body=body, headers=headers)
            resp = conn.getresponse()
            # 超限多读一个字节：读满即视为超限，不假装读完。
            raw = resp.read(_MAX_RESPONSE_BYTES + 1)
            if len(raw) > _MAX_RESPONSE_BYTES:
                raise EmbeddingError("shape", "端点响应超过字节上限")
            return resp.status, raw
        except TimeoutError as exc:
            raise EmbeddingError(
                "timeout", f"端点超时（{self._timeout_s}s）：{exc}"
            ) from exc
        except (OSError, http.client.HTTPException) as exc:
            raise EmbeddingError("network", f"端点网络失败：{exc}") from exc
        finally:
            conn.close()

    def _parse_vectors(self, raw: bytes, *, expected: int) -> list[list[float]]:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EmbeddingError("shape", f"端点响应不是合法 JSON：{exc}") from exc
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list) or len(data) != expected:
            actual = len(data) if isinstance(data, list) else "非数组"
            raise EmbeddingError(
                "shape", f"端点响应 data 缺失或条数不符：期望 {expected}，实际 {actual}"
            )
        by_index: dict[int, list[float]] = {}
        for position, item in enumerate(data):
            if not isinstance(item, dict) or "embedding" not in item:
                raise EmbeddingError(
                    "shape", f"端点响应第 {position} 项缺 embedding 字段"
                )
            index = item.get("index", position)
            if isinstance(index, bool) or not isinstance(index, int):
                raise EmbeddingError(
                    "shape", f"端点响应第 {position} 项 index 非整数：{index!r}"
                )
            vector = item["embedding"]
            if not isinstance(vector, list) or any(
                isinstance(v, bool) or not isinstance(v, (int, float)) for v in vector
            ):
                raise EmbeddingError(
                    "shape", f"端点响应第 {position} 项 embedding 不是数字数组"
                )
            if len(vector) != self.dim:
                raise EmbeddingError(
                    "shape",
                    f"端点返回维度 {len(vector)}，与配置 dim={self.dim} 不符"
                    "（换模型必须同步改 TIANSHU_EMBEDDING_DIM 并重建索引）",
                )
            by_index[index] = [float(v) for v in vector]
        if sorted(by_index) != list(range(expected)):
            raise EmbeddingError(
                "shape", f"端点响应 index 不连续：{sorted(by_index)!r}"
            )
        return [by_index[i] for i in range(expected)]

    def _digest(self, raw: bytes) -> str:
        text = raw[: _MAX_ERROR_BODY * 4].decode("utf-8", errors="replace").strip()
        if not text:
            return ""
        suffix = " …" if len(text) > _MAX_ERROR_BODY else ""
        return f"：{text[:_MAX_ERROR_BODY]}{suffix}"


def from_env() -> OpenAICompatibleEmbeddingProvider:
    """读环境变量四件套装配 provider；缺一报错并点名缺什么。"""
    missing = [
        name
        for name in (ENV_BASE_URL, ENV_MODEL, ENV_DIM, ENV_API_KEY)
        if not os.environ.get(name, "").strip()
    ]
    if missing:
        raise TianshuError(
            "外部 embedding provider 配置不完整，缺少环境变量："
            + "、".join(missing)
            + "（凭据只从环境变量读，永不落盘）"
        )
    raw_dim = os.environ[ENV_DIM].strip()
    try:
        dim = int(raw_dim)
    except ValueError as exc:
        raise TianshuError(f"{ENV_DIM} 必须是正整数：{raw_dim!r}") from exc
    return OpenAICompatibleEmbeddingProvider(
        base_url=os.environ[ENV_BASE_URL].strip(),
        model=os.environ[ENV_MODEL].strip(),
        dim=dim,
        api_key=os.environ[ENV_API_KEY].strip(),
    )
