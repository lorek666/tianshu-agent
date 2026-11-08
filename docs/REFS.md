# 参考仓库（已固定的 commit）

这些仓库用于架构参考，**不进版本库**（合计 762MB）。在本地任意位置克隆后按以下 commit 检出，保证研究可复现。

| 目录 | 仓库 | commit | 日期 |
|---|---|---|---|
| `strands` | strands-agents/sdk-python | `3d32c2269609` | 2026-11-06 |
| `deepagents` | langchain-ai/deepagents | `4182b6f89ddc` | 2026-11-08 |
| `openhands` | OpenHands/OpenHands | `b8e87db15c5e` | 2026-11-08 |
| `oh-sdk` | OpenHands/software-agent-sdk | `51ad22388a15` | 2026-11-07 |
| `mcp-python-sdk` | modelcontextprotocol/python-sdk | `77001c96f950` | 2026-11-05 |
| `pydantic-ai` | pydantic/pydantic-ai | `0f7648a1b068` | 2026-11-08 |

## 重新获取

```bash
mkdir -p refs && cd refs
git clone --depth 50 https://github.com/strands-agents/sdk-python.git strands
git -C strands checkout 3d32c2269609
# 其余同理
```

## 本地已有副本

```
/Users/ronfee/.dsh/bot/workspace/agentos/refs/
```

## 每个仓库看什么

| 仓库 | 只看这些 |
|---|---|
| `strands` | `types/event_loop.py`（FinishReason 13 值）、`types/agent.py`（Limits 四键与继承语义）、`session/`（状态外置） |
| `deepagents` | `middleware/_blob_offload.py`（内容寻址卸载）、`middleware/_video.py`（抽帧图文块，v2 用）、`middleware/subagents.py`（isolated/fork） |
| `oh-sdk` | `conversation_lease.py`（租约 + PID 校验）、`bash_service.py:414-424`（SIGTERM→进程组→优雅期→SIGKILL） |
| `mcp-python-sdk` | `shared/context.py`（cancel_requested 是 anyio.Event，协作式）、版本区间协商 |
| `pydantic-ai` | `images/base.py`（GenerationModel 与 ChatModel 平行继承，v2 用） |

⚠️ **`openhands` 主仓库只有 17 个 .py** —— runtime 在 `oh-sdk`。审主仓库等于白干。
