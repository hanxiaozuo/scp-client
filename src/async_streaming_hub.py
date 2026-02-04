#!/usr/bin/env python3
"""
MCP Hub Server - 最终完整版本
功能：
1. 使用 asyncio.Event 进行正确的异步同步（流式输出工作）
2. 支持默认参数（None 值不传给后端）
3. 错误通知（流式工具出错时通知客户端）
"""

import asyncio
import json
import uuid
from datetime import datetime
from typing import Any, Dict, List, Literal, Callable
from fastmcp import FastMCP, Context
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import httpx
from mcp.client.sse import sse_client
from mcp.types import LoggingMessageNotification, LoggingMessageNotificationParams

import inspect
from types import FunctionType

# 初始化 Hub MCP server
hub = FastMCP("mcp-hub-server")

# 配置后端 MCP servers
BACKEND_SERVERS = [
    {
        "name": "async_server",
        "url": "http://localhost:2800/sse",
        "transport": "sse",
        "description": "MCP Server - 101.126.20.41"
    },
    {
        "name": "tooluniverse",
        "url": "http://180.184.86.2:32209/mcp",
        "transport": "streamable-http",
        "description": "MCP Server - 115.190.136.251"
    },
    {
        "name": "stream_output",
        "url": "http://localhost:8000/mcp",
        "transport": "streamable-http",
        "description": "流式输出服务器",
        "streaming": True  # 标记为流式服务器
    }
]

class AsyncTaskManager:
    """异步任务管理器"""
    
    def __init__(self, sync_timeout: int = 5):
        self.task_storage: Dict[str, Dict[str, Any]] = {}
        self.sync_timeout = sync_timeout

    async def execute(
        self, 
        func: Callable,
        enable_async: bool = False,
        *args, 
        **kwargs
    ) -> Dict[str, Any]:
        if not enable_async:
            try:
                result = await func(*args, **kwargs)
                return {
                    "status": "completed",
                    "result": result,
                    "mode": "sync"
                }
            except Exception as e:
                import traceback
                return {
                    "status": "failed",
                    "error": str(e),
                    "traceback": traceback.format_exc(),
                    "mode": "sync"
                }
        
        task_id = str(uuid.uuid4())
        self.task_storage[task_id] = {
            "task_id": task_id,
            "status": "processing",
            "created_at": datetime.now().isoformat(),
            "result": None,
            "error": None
        }

        async def task_wrapper():
            try:
                result = await func(*args, **kwargs)
                self.task_storage[task_id].update({
                    "status": "completed",
                    "result": result,
                    "message": "任务已完成",
                    "completed_at": datetime.now().isoformat()
                })
                if 'info' in self.task_storage[task_id]:
                    del self.task_storage[task_id]['info']
                return result
            except Exception as e:
                import traceback
                self.task_storage[task_id].update({
                    "status": "failed",
                    "error": str(e),
                    "traceback": traceback.format_exc(),
                    "message": "任务失败",
                    "failed_at": datetime.now().isoformat()
                })
                if 'info' in self.task_storage[task_id]:
                    del self.task_storage[task_id]['info']
                raise

        task = asyncio.create_task(task_wrapper())

        try:
            result = await asyncio.wait_for(
                asyncio.shield(task),
                timeout=self.sync_timeout
            )
            response = self.task_storage[task_id].copy()
            del self.task_storage[task_id]
            return response
        except asyncio.TimeoutError:
            self.task_storage[task_id].update({
                "message": "任务已提交，耗时较长，转为后台执行",
                "info": "请稍后使用 get_task_status 工具查询结果"
            })
            return self.task_storage[task_id]
        except Exception as e:
            response = self.task_storage[task_id].copy()
            return response

    def get_status(self, task_id: str) -> Dict[str, Any]:
        if task_id not in self.task_storage:
            return {
                "message": f"未找到任务ID: {task_id}",
                "status": "not_found"
            }
        
        task_data = self.task_storage[task_id]
        status = task_data.get("status")

        if status in ["completed", "failed"]:
            result = task_data.copy()
            del self.task_storage[task_id]
            return result

        return task_data
    
    def list_tasks(self) -> List[Dict[str, Any]]:
        return [
            {
                "task_id": task_id,
                "status": task_data.get("status"),
                "created_at": task_data.get("created_at"),
                "message": task_data.get("message", "")
            }
            for task_id, task_data in self.task_storage.items()
        ]


class MCPClient:
    """MCP 客户端 - 支持多用户并发的流式通知转发"""
    
    def __init__(
        self, 
        server_url: str, 
        server_name: str, 
        transport: Literal["streamable-http", "sse"] = "streamable-http",
        headers_config: dict = {},
        is_streaming: bool = False,
    ):
        self.server_url = server_url
        self.server_name = server_name
        self.transport_type = transport
        self.headers_config = headers_config
        self.is_streaming = is_streaming
        self.session = None
        self.transport = None
        self.session_ctx = None
        self.tools_cache = None
        self._connected = False
        self._connection_lock = asyncio.Lock()
        
        # 🆕 多用户支持：使用字典存储每个请求的 hub_session
        # key: request_id, value: {"hub_session": session, "event": Event}
        self._active_streams: Dict[str, Dict] = {}
        self._streams_lock = asyncio.Lock()
    
    async def register_stream(self, request_id: str, hub_session) -> asyncio.Event:
        """注册一个流式请求"""
        async with self._streams_lock:
            event = asyncio.Event()
            self._active_streams[request_id] = {
                "hub_session": hub_session,
                "event": event
            }
            return event
    
    async def unregister_stream(self, request_id: str):
        """注销一个流式请求"""
        async with self._streams_lock:
            if request_id in self._active_streams:
                stream_info = self._active_streams[request_id]
                stream_info["event"].set()  # 确保等待的协程被释放
                del self._active_streams[request_id]
    
    async def send_error_notification(self, request_id: str, error_msg: str):
        """🆕 发送错误通知到指定请求"""
        async with self._streams_lock:
            stream_info = self._active_streams.get(request_id)
            if stream_info and stream_info["hub_session"]:
                try:
                    notif = LoggingMessageNotification(
                        method="notifications/message",
                        params=LoggingMessageNotificationParams(
                            level="error",
                            data={
                                "type": "stream_error",
                                "error": error_msg,
                            }
                        )
                    )
                    await stream_info["hub_session"].send_notification(notif)
                    print(f"[{self.server_name}] 已发送错误通知到 {request_id[:8]}...")
                except Exception as e:
                    print(f"[{self.server_name}] 发送错误通知失败: {e}")
    
    async def handle_notification(self, message):
        """处理来自后端的通知并转发给所有活跃的流式请求"""
        if not self.is_streaming:
            return
        
        # 转发通知给所有活跃的流式请求
        async with self._streams_lock:
            if not self._active_streams:
                # 没有活跃的流式请求，静默跳过
                return
            
            try:
                # 解析通知
                if hasattr(message, 'root'):
                    actual = message.root
                else:
                    actual = message
                
                method = getattr(actual, "method", "")
                params = getattr(actual, "params", None)
                
                if method == "notifications/message" and params:
                    data = getattr(params, 'data', None) or {}
                    level = getattr(params, 'level', 'info')
                    
                    # 检查是否是流式完成通知
                    is_complete = False
                    data_type = ""
                    if isinstance(data, dict):
                        data_type = data.get("type", "")
                        if data_type == "stream_complete":
                            is_complete = True
                    
                    # 创建新的通知对象
                    notif = LoggingMessageNotification(
                        method="notifications/message",
                        params=LoggingMessageNotificationParams(
                            level=level,
                            data=data
                        )
                    )
                    
                    # 转发给所有活跃的流式请求
                    for request_id, stream_info in list(self._active_streams.items()):
                        try:
                            await stream_info["hub_session"].send_notification(notif)
                            if is_complete:
                                stream_info["event"].set()
                        except Exception as e:
                            print(f"[{self.server_name}] 转发通知失败: {e}")
                    
            except Exception as e:
                import traceback
                print(f"[{self.server_name}] ⚠️ 处理通知出错: {e}")
                traceback.print_exc()
    
    async def connect(self, force_reconnect: bool = False):
        """连接到 MCP 服务器"""
        # 🆕 使用连接锁防止并发连接问题
        async with self._connection_lock:
            if self._connected and not force_reconnect:
                # 🔧 验证连接是否真的可用（对所有类型的服务器）
                if self.session:
                    try:
                        # 尝试列出工具来验证连接
                        await self.session.list_tools()
                        return True
                    except Exception as e:
                        print(f"  ⚠️ {self.server_name} 连接已断开，正在重连...")
                        self._connected = False
                        # 清理旧连接
                        await self._disconnect_internal()
                else:
                    return True
            
            # 🔧 如果是强制重连，先断开现有连接
            if force_reconnect and self._connected:
                print(f"  🔄 {self.server_name} 强制重连...")
                await self._disconnect_internal()
                
            try:
                print(f"  🔗 连接到 {self.server_name} ({self.transport_type})...")
                
                if self.transport_type == "streamable-http":
                    self.transport = streamable_http_client(
                        url=self.server_url,
                        http_client=httpx.AsyncClient(headers=self.headers_config)
                    )
                    self.read, self.write, self.get_session_id = await self.transport.__aenter__()
                elif self.transport_type == "sse":
                    self.transport = sse_client(self.server_url, headers=self.headers_config)
                    self.read, self.write = await self.transport.__aenter__()
                else:
                    raise ValueError(f"不支持的传输类型: {self.transport_type}")
                
                if self.is_streaming:
                    self.session_ctx = ClientSession(
                        self.read, 
                        self.write, 
                        message_handler=self.handle_notification
                    )
                else:
                    self.session_ctx = ClientSession(self.read, self.write)
                
                self.session = await self.session_ctx.__aenter__()
                await self.session.initialize()
                
                # 🔧 设置 notification_active 为 True（对于流式服务器）
                if self.is_streaming:
                    self.notification_active = True
                
                self._connected = True
                streaming_mark = "🌊 [流式]" if self.is_streaming else ""
                print(f"  ✓ {self.server_name} {streaming_mark} 连接成功")
                return True
                
            except Exception as e:
                print(f"  ✗ {self.server_name} 连接失败: {e}")
                self._connected = False
                return False
    
    async def _disconnect_internal(self):
        """内部断开连接方法（不获取锁）"""
        try:
            if self.session_ctx:
                await self.session_ctx.__aexit__(None, None, None)
                self.session_ctx = None
                self.session = None
            
            if self.transport:
                await self.transport.__aexit__(None, None, None)
                self.transport = None
            
            self._connected = False
        except Exception as e:
            print(f"  ✗ 断开 {self.server_name} 连接时出错: {e}")
    
    async def disconnect(self):
        """断开连接"""
        try:
            if self.session_ctx:
                await self.session_ctx.__aexit__(None, None, None)
                self.session_ctx = None
                self.session = None
            
            if self.transport:
                await self.transport.__aexit__(None, None, None)
                self.transport = None
            
            self._connected = False
        except Exception as e:
            print(f"  ✗ 断开 {self.server_name} 连接时出错: {e}")
    
    async def list_tools(self) -> List[Dict]:
        """获取服务器的工具列表"""
        if not self._connected:
            await self.connect()
        
        if not self._connected:
            return []
        
        try:
            tools_list = await self.session.list_tools()
            tools = []
            for tool in tools_list.tools:
                tools.append({
                    "name": tool.name,
                    "description": tool.description if hasattr(tool, 'description') else "",
                    "inputSchema": tool.inputSchema if hasattr(tool, 'inputSchema') else {}
                })
            self.tools_cache = tools
            return tools
        except Exception as e:
            print(f"  ✗ 获取 {self.server_name} 工具列表失败: {e}")
            return []
    
    async def call_tool(self, tool_name: str, arguments: Dict = None) -> Any:
        """调用工具
        
        Args:
            tool_name: 工具名称
            arguments: 工具参数
        """
        if not self._connected:
            await self.connect()
        
        if not self._connected:
            raise Exception(f"无法连接到服务器 {self.server_name}")
        
        try:
            result = await self.session.call_tool(tool_name, arguments or {})
            
            if hasattr(result, 'content') and result.content:
                if len(result.content) == 1:
                    content_item = result.content[0]
                    if hasattr(content_item, 'text'):
                        return content_item.text
                
                return [
                    getattr(item, 'text', str(item))
                    for item in result.content
                ]
            
            return str(result)
            
        except Exception as e:
            # 如果调用失败，标记连接为断开状态，下次会重连
            self._connected = False
            raise Exception(f"调用 {self.server_name}.{tool_name} 失败: {str(e)}")


# 全局变量
mcp_clients: Dict[str, MCPClient] = {}
task_manager = AsyncTaskManager(sync_timeout=5)


def init_mcp_clients():
    """初始化所有 MCP 客户端"""
    global mcp_clients
    
    for server in BACKEND_SERVERS:
        client = MCPClient(
            server_url=server["url"],
            server_name=server["name"],
            transport=server.get("transport", "streamable-http"),
            headers_config={},
            is_streaming=server.get("streaming", False)
        )
        mcp_clients[server["name"]] = client


async def get_all_backend_tools() -> Dict[str, Dict]:
    """获取所有后端服务器的工具"""
    all_tools = {}
    
    for server_name, client in mcp_clients.items():
        tools = await client.list_tools()
        for tool in tools:
            tool_name = tool.get("name")
            if tool_name:
                prefixed_name = f"{server_name}_{tool_name}"
                all_tools[prefixed_name] = {
                    "server": server_name,
                    "original_name": tool_name,
                    "tool_info": tool,
                    "is_streaming": client.is_streaming
                }
    
    return all_tools


def build_proxy_tool(
    srv_name: str,
    orig_name: str,
    tool_info: dict,
    is_streaming: bool = False,
):
    """
    根据后端 MCP tool 的 inputSchema 动态生成工具函数
    🔑 关键：使用 asyncio.Event 等待完成
    """
    schema = tool_info.get("inputSchema", {})
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))

    parameters = []
    annotations = {}

    parameters.append(
        inspect.Parameter(
            "ctx",
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    )
    annotations["ctx"] = Context

    for name, prop in properties.items():
        if name in required:
            parameters.append(
                inspect.Parameter(
                    name,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                )
            )
        else:
            parameters.append(
                inspect.Parameter(
                    name,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    default=None,
                )
            )
        annotations[name] = Any

    if not is_streaming:
        parameters.append(
            inspect.Parameter(
                "_enable_async",
                inspect.Parameter.KEYWORD_ONLY,
                default=False,
            )
        )
        annotations["_enable_async"] = bool

    async def _impl(**kwargs):
        ctx = kwargs.pop("ctx")
        client = mcp_clients[srv_name]
        
        # 🆕 过滤掉值为 None 的参数，让后端使用默认值
        filtered_kwargs = {k: v for k, v in kwargs.items() if v is not None and k != "_enable_async"}
        
        if is_streaming:
            # 获取 Hub ServerSession
            hub_session = None
            
            # 方法1: 从 request_context 获取
            if hasattr(ctx, 'request_context') and ctx.request_context:
                if hasattr(ctx.request_context, '_session'):
                    hub_session = ctx.request_context._session
                elif hasattr(ctx.request_context, 'session'):
                    hub_session = ctx.request_context.session
            
            # 方法2: 从 _fastmcp_server 获取
            if not hub_session and hasattr(ctx, '_fastmcp_server'):
                server = ctx._fastmcp_server
                if hasattr(server, '_session'):
                    hub_session = server._session
            
            # 方法3: 遍历 Context 属性查找
            if not hub_session:
                for attr in dir(ctx):
                    if 'session' in attr.lower() and not attr.startswith('__'):
                        try:
                            val = getattr(ctx, attr)
                            if val and hasattr(val, 'send_notification'):
                                hub_session = val
                                break
                        except Exception:
                            pass
            
            if hub_session:
                # 🆕 多用户支持：为每个请求生成唯一ID
                request_id = str(uuid.uuid4())
                
                # 🔧 关键修复：为每次流式调用创建独立的临时连接
                # 这样可以避免异步服务调用影响流式服务的连接状态
                temp_client = MCPClient(
                    server_url=client.server_url,
                    server_name=f"{client.server_name}_temp_{request_id[:8]}",
                    transport=client.transport_type,
                    headers_config=client.headers_config,
                    is_streaming=True
                )
                
                try:
                    # 建立临时连接
                    await temp_client.connect()
                    
                    # 注册流式请求到临时客户端
                    await temp_client.register_stream(request_id, hub_session)
                    
                    result = await temp_client.call_tool(orig_name, arguments=filtered_kwargs)
                    
                    # 给一个短暂延迟，确保所有通知都已转发
                    await asyncio.sleep(0.1)
                    
                    return result
                
                except Exception as e:
                    error_msg = f"工具调用失败: {str(e)}"
                    await temp_client.send_error_notification(request_id, error_msg)
                    await asyncio.sleep(0.1)
                    raise
                    
                finally:
                    # 注销流式请求并断开临时连接
                    await temp_client.unregister_stream(request_id)
                    await temp_client.disconnect()
            else:
                return await client.call_tool(orig_name, arguments=filtered_kwargs)
        
        # 非流式工具
        enable_async = kwargs.pop("_enable_async", False)
        
        async def call_backend():
            return await client.call_tool(orig_name, arguments=filtered_kwargs)
        
        return await task_manager.execute(call_backend, enable_async)

    sig = inspect.Signature(parameters)

    proxy = FunctionType(
        _impl.__code__,
        globals(),
        name=f"proxy_{srv_name}_{orig_name}",
        argdefs=None,
        closure=_impl.__closure__,
    )

    proxy.__signature__ = sig
    proxy.__annotations__ = annotations
    
    if is_streaming:
        proxy.__doc__ = f"""🌊 [流式工具 - 来自 {srv_name}] {tool_info.get('description', '')}

参数:
  - ctx: Context (自动注入)
{chr(10).join(f"  - {name}: {prop.get('description', prop.get('type', 'any'))}" for name, prop in properties.items())}

⚠️ 此工具支持实时流式输出
"""
    else:
        proxy.__doc__ = f"""[来自 {srv_name}] {tool_info.get('description', '')}

参数:
  - ctx: Context (自动注入)
{chr(10).join(f"  - {name}: {prop.get('description', prop.get('type', 'any'))}" for name, prop in properties.items())}
  - _enable_async (可选): 是否启用异步模式，默认False
"""

    return proxy


async def register_backend_tools():
    """动态注册所有后端 MCP server 的工具"""
    backend_tools = await get_all_backend_tools()
    
    for prefixed_name, tool_data in backend_tools.items():
        server_name = tool_data["server"]
        original_name = tool_data["original_name"]
        tool_info = tool_data["tool_info"]
        is_streaming = tool_data.get("is_streaming", False)
        
        tool_func = build_proxy_tool(server_name, original_name, tool_info, is_streaming)
        hub.tool()(tool_func)
        
        streaming_mark = "🌊" if is_streaming else "✓"
        print(f"  {streaming_mark} 已注册工具: {tool_func.__name__} (来自 {server_name})")


# ==================== Hub 管理工具 ====================

@hub.tool()
async def list_backend_servers(ctx: Context) -> List[Dict]:
    """列出所有后端 MCP servers 及其状态"""
    servers_status = []
    
    for server in BACKEND_SERVERS:
        client = mcp_clients.get(server["name"])
        if client:
            tools = await client.list_tools()
            
            servers_status.append({
                "name": server["name"],
                "url": server["url"],
                "transport": server.get("transport", "streamable-http"),
                "description": server["description"],
                "status": "online" if client._connected else "offline",
                "tools_count": len(tools),
                "tools": [t.get("name") for t in tools],
                "streaming": server.get("streaming", False)
            })
        else:
            servers_status.append({
                "name": server["name"],
                "url": server["url"],
                "transport": server.get("transport", "streamable-http"),
                "description": server["description"],
                "status": "not_initialized",
                "tools_count": 0,
                "tools": [],
                "streaming": server.get("streaming", False)
            })
    
    return servers_status


@hub.tool()
async def get_tool_mapping(ctx: Context) -> Dict[str, Dict]:
    """获取工具名称映射关系"""
    backend_tools = await get_all_backend_tools()
    return {
        prefixed_name: {
            "server": data["server"],
            "original_name": data["original_name"],
            "description": data["tool_info"].get("description", ""),
            "is_streaming": data.get("is_streaming", False)
        }
        for prefixed_name, data in backend_tools.items()
    }


@hub.tool()
def get_task_status(task_id: str) -> Dict[str, Any]:
    """查询异步任务的执行状态"""
    return task_manager.get_status(task_id)


@hub.tool()
def list_all_tasks() -> List[Dict[str, Any]]:
    """列出所有正在执行或待查询的异步任务"""
    return task_manager.list_tasks()


@hub.tool()
def set_async_timeout(timeout_seconds: int) -> Dict[str, Any]:
    """设置异步任务的超时时间"""
    if timeout_seconds < 1:
        return {
            "success": False,
            "message": "超时时间必须大于等于1秒"
        }
    
    old_timeout = task_manager.sync_timeout
    task_manager.sync_timeout = timeout_seconds

    return {
        "success": True,
        "old_timeout": old_timeout,
        "new_timeout": timeout_seconds,
        "message": f"异步超时时间已从 {old_timeout}秒 更新为 {timeout_seconds}秒"
    }


# 初始化客户端
init_mcp_clients()


# 使用 lifespan 上下文管理器
from contextlib import asynccontextmanager
from typing import AsyncIterator

@asynccontextmanager
async def lifespan(server) -> AsyncIterator[dict]:
    """服务器生命周期管理"""
    print("=" * 80)
    print("MCP Hub Server (Final Complete Version) - 正在启动...")
    print("=" * 80)
    print(f"\n✓ 已配置 {len(BACKEND_SERVERS)} 个后端服务器")
    
    streaming_count = sum(1 for s in BACKEND_SERVERS if s.get("streaming", False))
    print(f"✓ 其中流式服务器: {streaming_count} 个")
    print(f"✓ 功能: 流式输出 + 默认参数 + 错误通知")
    
    print(f"✓ 异步任务超时设置: {task_manager.sync_timeout}秒")
    
    print("\n正在从后端服务器加载和注册工具...")
    await register_backend_tools()
    
    print("\n" + "=" * 80)
    print("✓ Hub Server 初始化完成")
    print("=" * 80)
    
    yield {}
    
    print("\n" + "=" * 80)
    print("MCP Hub Server - 正在关闭...")
    print("=" * 80)
    
    for server_name, client in mcp_clients.items():
        await client.disconnect()
    
    print("✓ 所有连接已关闭")
    print("=" * 80)


hub._lifespan = lifespan


if __name__ == "__main__":
    hub.run(transport="streamable-http", host="0.0.0.0", port=18082)