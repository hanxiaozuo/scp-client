from mcp.client.streamable_http import streamable_http_client
from mcp.client.sse import sse_client
from mcp import ClientSession
import httpx
import uuid
import json
from typing import Literal


class fetch_scp_Client:
    """使用MCP SDK的非流式客户端"""
  
    def __init__(self, server_url: str, transport_type: Literal["streamable-http", "sse"] = "streamable-http", headers_config: dict={}):
        self.server_url = server_url
        self.session = None
        self.transport_type = transport_type
        self.headers_config = headers_config

    async def connect(self):
        """建立连接并初始化会话"""
        print(f"\n{'='*80}")
        print("连接到MCP服务器")
        print(f"{'='*80}")
        print(f"服务器地址: {self.server_url}")

        try:
            # 建立streamable-http传输连接
            if self.transport_type == "streamable-http":
                self.transport = streamable_http_client(
                    url=self.server_url, 
                    http_client=httpx.AsyncClient(headers = self.headers_config)
                    )
                self.read, self.write, self.get_session_id = await self.transport.__aenter__()
                session_id = self.get_session_id()
            
            # 建立sse传输连接
            elif self.transport_type == "sse":
                self.transport = sse_client(url=self.server_url, headers=self.headers_config)
                self.read, self.write = await self.transport.__aenter__()
                session_id = str(uuid.uuid4())

            else:
                raise ValueError(f"不支持的传输类型: {self.transport_type}")

            # 创建客户端会话
            self.session_ctx = ClientSession(self.read, self.write)
            self.session = await self.session_ctx.__aenter__()  

            # 初始化会话
            await self.session.initialize()
            print(f"✓ 连接成功")
            print(f"✓ 会话ID: {session_id}")
            print(f"{'='*80}\n")
            return True
            
        except Exception as e:
            print(f"✗ 连接失败: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    async def disconnect(self):
        """断开连接"""
        try:
            if self.session:
                await self.session_ctx.__aexit__(None, None, None)
            if hasattr(self, 'transport'):
                await self.transport.__aexit__(None, None, None)
            print("\n✓ 已断开连接\n")
        except Exception as e:
            print(f"✗ 断开连接时出错: {e}")
    
    async def list_tools(self):
        """列出所有可用工具"""
        print(f"\n{'='*80}")
        print("列出所有可用工具")
        print(f"{'='*80}")
        
        try:
            tools_list = await self.session.list_tools()
            print(f"\n可用工具 (共{len(tools_list.tools)}个):\n")
            
            for i, tool in enumerate(tools_list.tools, 1):
                print(f"{i:2d}. {tool.name}")
                if tool.description:
                    desc_line = tool.description
                    print(f"    {desc_line}")
            
            print(f"\n✓ 工具列表获取成功")
            return tools_list.tools
            
        except Exception as e:
            print(f"✗ 列出工具失败: {e}")
            return []

    def parse_result(self, result):
        """解析MCP工具调用结果"""
        try:
            if hasattr(result, 'content') and result.content:
                content = result.content[0]
                if hasattr(content, 'text'):
                    return json.loads(content.text)
                
            return str(result)
        except Exception as e:
            return {"error": f"解析结果失败: {e}", "raw": str(result)}


class stream_scp_Client:
    """使用MCP SDK的流式客户端"""

    def __init__(self, server_url: str, headers_config: dict={}):
        self.server_url = server_url
        self.session = None
        self.headers_config = headers_config

    async def handle_notification(self, message):
        """处理通知"""
        streaming_buffer = []
        # 解析通知对象
        if hasattr(message, 'root'):
            actual = message.root
        else:
            actual = message
        
        method = getattr(actual, "method", "")
        params = getattr(actual, "params", None)
        
        # 只处理 notifications/message
        if method == "notifications/message" and params:
            data = getattr(params, 'data', None) or {}
            
            if isinstance(data, dict):
                data_type = data.get("type", "")
                
                if data_type == "stream_chunk":
                    # 实时显示流式内容
                    content = data.get("content", "")
                    print(content, end="", flush=True)
                    streaming_buffer.append(content)
                    
                elif data_type == "stream_complete":
                    # 流式完成
                    print()
    
    async def connect(self):
        """建立连接并初始化会话"""
        print(f"\n{'='*80}")
        print("连接到MCP服务器")
        print(f"{'='*80}")
        print(f"服务器地址: {self.server_url}")

        try:
            # 建立streamable-http传输连接
            self.transport = streamable_http_client(
                    url=self.server_url, 
                    http_client=httpx.AsyncClient(headers = self.headers_config)
                    )
            self.read, self.write, self.get_session_id = await self.transport.__aenter__()
            
            # 创建客户端会话
            self.session_ctx = ClientSession(self.read, self.write, message_handler=self.handle_notification)
            self.session = await self.session_ctx.__aenter__()
            
            # 初始化会话
            await self.session.initialize()
            session_id = self.get_session_id()
            
            print(f"✓ 连接成功")
            print(f"✓ 会话ID: {session_id}")
            print(f"{'='*80}\n")
            return True
            
        except Exception as e:
            print(f"✗ 连接失败: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    async def disconnect(self):
        """断开连接"""
        try:
            if self.session:
                await self.session_ctx.__aexit__(None, None, None)
            if hasattr(self, 'transport'):
                await self.transport.__aexit__(None, None, None)
            print("\n✓ 已断开连接\n")
        except Exception as e:
            print(f"✗ 断开连接时出错: {e}")
    
    async def list_tools(self):
        """列出所有可用工具"""
        print(f"\n{'='*80}")
        print("列出所有可用工具")
        print(f"{'='*80}")
        
        try:
            tools_list = await self.session.list_tools()
            print(f"\n可用工具 (共{len(tools_list.tools)}个):\n")
            
            for i, tool in enumerate(tools_list.tools, 1):
                print(f"{i:2d}. {tool.name}")
                if tool.description:
                    desc_line = tool.description
                    print(f"    {desc_line}")
            
            print(f"\n✓ 工具列表获取成功")
            return tools_list.tools
            
        except Exception as e:
            print(f"✗ 列出工具失败: {e}")
            return []
    
