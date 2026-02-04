# scp-client

面向 **SCP（Science Context Protocol）** 相关服务的客户端调用工具（Client utilities）。

SCP 介绍与协议细节请参考：<https://github.com/InternScience/scp>

---

This repository provides **client-side utilities** for services related to **SCP (Science Context Protocol)**.

For SCP details, see: <https://github.com/InternScience/scp>

## 特性 / Features

- 支持两种传输类型：`streamable-http` / `sse`
- 非流式调用（一次性返回结果）
- 流式调用（通过 `notifications/message` 实时输出）

---

- Supports two transport types: `streamable-http` / `sse`
- Non-streaming calls (single final result)
- Streaming calls (real-time output via `notifications/message`)

## 目录结构 / Layout

```text
.
├── requirements.txt
└── src/
    ├── async_streaming_hub.py
    └── scp_client_files/
        └── scp_client.py
```

> 说明：`src/async_streaming_hub.py` 是一个多后端 MCP/SCP 服务聚合 Hub 的开发中示例（WIP），目前仓库主推与主要维护的是 client 工具。

> Note: `src/async_streaming_hub.py` is an **in-progress (WIP)** example of a multi-backend MCP/SCP aggregation hub. The primary focus of this repo is the client utilities.

## 快速开始 / Quickstart

### 1) 安装依赖 / Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
```

### 2) Client 使用示例 / Client usage examples

文件：`src/scp_client_files/scp_client.py`

#### 非流式客户端（支持 streamable-http / sse）

```python
import asyncio
from src.scp_client_files.scp_client import fetch_scp_Client


async def main():
    client = fetch_scp_Client(
        server_url="https://your-mcp-server.example.com/mcp",
        transport_type="streamable-http",  # or "sse"
        headers_config={},
    )
    await client.connect()
    await client.list_tools()
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
```

#### 流式客户端（streamable-http）

```python
import asyncio
from src.scp_client_files.scp_client import stream_scp_Client


async def main():
    client = stream_scp_Client(
        server_url="https://your-mcp-server.example.com/mcp",
        headers_config={},
    )
    await client.connect()
    await client.list_tools()
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
```

## 参考 / References

- SCP (Science Context Protocol): <https://github.com/InternScience/scp>
