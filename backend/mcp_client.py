"""
MCP (Model Context Protocol) Client for Nexus Dashboard Integration
====================================================================

This module provides a client for connecting to the ND MCP Server via SSE
(Server-Sent Events) and executing Nexus Dashboard operations as tools using
the JSON-RPC 2.0 protocol.

Features:
---------
- SSE connection with JSON-RPC 2.0 messaging
- Tool discovery via tools/list
- Tool execution via tools/call
- Async request/response handling
- Error handling and retries
- Read-only operation filtering

"""

import os
import json
import asyncio
import aiohttp
from typing import Dict, List, Optional, Any, Callable
from datetime import datetime, timedelta
import structlog

logger = structlog.get_logger(__name__)


class MCPError(Exception):
    """Base exception for MCP protocol errors"""
    pass


class MCPClient:
    """
    Client for connecting to MCP Server via SSE with JSON-RPC 2.0 protocol.
    """

    def __init__(
        self,
        url: str,
        token: str,
        ssl_verify: bool = False,
        timeout: int = 30,
        reconnect_delay: int = 5
    ):
        """
        Initialize MCP Client.

        Args:
            url: MCP server URL (e.g., https://nd-mcp-server-gbaia.apps.fp-ocp.amsdmz.local)
            token: Bearer token for authentication
            ssl_verify: Whether to verify SSL certificates
            timeout: Request timeout in seconds
            reconnect_delay: Delay between reconnection attempts
        """
        self.url = url.rstrip('/')
        self.token = token
        self.ssl_verify = ssl_verify
        self.timeout = timeout
        self.reconnect_delay = reconnect_delay

        self.session: Optional[aiohttp.ClientSession] = None
        self.sse_task: Optional[asyncio.Task] = None

        self.tools: Dict[str, Dict[str, Any]] = {}
        self.tools_last_updated: Optional[datetime] = None
        self.connected = False

        # JSON-RPC request tracking
        self._request_id = 0
        self._pending_requests: Dict[int, asyncio.Future] = {}

        # SSE connection
        self._sse_reader: Optional[asyncio.StreamReader] = None
        self._should_reconnect = True

    def _next_request_id(self) -> int:
        """Generate next JSON-RPC request ID"""
        self._request_id += 1
        return self._request_id

    async def connect(self):
        """
        Initialize HTTP session and establish SSE connection.
        """
        if self.session is None:
            connector = aiohttp.TCPConnector(ssl=self.ssl_verify)
            timeout = aiohttp.ClientTimeout(total=None)  # No timeout for SSE
            self.session = aiohttp.ClientSession(connector=connector, timeout=timeout)

        try:
            # Check server health first
            await self._check_health()

            # Start SSE connection in background
            self.sse_task = asyncio.create_task(self._sse_connection_loop())

            # Wait a moment for connection to establish
            await asyncio.sleep(1)

            # Discover tools via JSON-RPC
            await self.discover_tools()

            self.connected = True
            logger.info("mcp_connected", url=self.url, tool_count=len(self.tools))
        except Exception as e:
            logger.error("mcp_connection_failed", error=str(e))
            raise

    async def disconnect(self):
        """
        Close SSE connection and HTTP session.
        """
        self._should_reconnect = False

        if self.sse_task:
            self.sse_task.cancel()
            try:
                await self.sse_task
            except asyncio.CancelledError:
                pass
            self.sse_task = None

        if self.session:
            await self.session.close()
            self.session = None

        self.connected = False
        logger.info("mcp_disconnected")

    async def _check_health(self) -> Dict[str, Any]:
        """
        Check MCP server health via REST API.
        """
        url = f"{self.url}/api/health"
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        async with self.session.get(url, timeout=timeout) as response:
            response.raise_for_status()
            data = await response.json()
            logger.debug("mcp_health_check", status=data.get("status"))
            return data

    async def _sse_connection_loop(self):
        """
        Maintain SSE connection and handle incoming messages.
        """
        while self._should_reconnect:
            try:
                await self._connect_sse()
            except asyncio.CancelledError:
                break
            except Exception as e:
                error_str = str(e)

                # TransferEncodingError is expected when SSE stream times out
                if "TransferEncodingError" in error_str or "Not enough data" in error_str:
                    logger.debug("sse_timeout", message="SSE stream timeout (expected)")
                else:
                    logger.error("sse_connection_error", error=error_str)

                if self._should_reconnect:
                    await asyncio.sleep(self.reconnect_delay)

    async def _connect_sse(self):
        """
        Establish SSE connection and process events.
        """
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "text/event-stream",
            "Cache-Control": "no-cache"
        }

        url = f"{self.url}/mcp/sse"
        logger.debug("connecting_sse", url=url)

        async with self.session.get(url, headers=headers) as response:
            if response.status != 200:
                error_text = await response.text()
                raise MCPError(f"SSE connection failed: {response.status} - {error_text}")

            logger.info("sse_connected")

            # Process SSE events
            async for line in response.content:
                try:
                    decoded_line = line.decode('utf-8').strip()

                    if not decoded_line:
                        continue

                    # SSE format: "event: <type>" or "data: <json>"
                    if decoded_line.startswith('event:'):
                        event_type = decoded_line.split(':', 1)[1].strip()
                        logger.debug("sse_event", type=event_type)

                    elif decoded_line.startswith('data:'):
                        data_str = decoded_line.split(':', 1)[1].strip()

                        # Skip non-JSON data (e.g., "/mcp/message" endpoint info)
                        if not data_str.startswith('{'):
                            logger.debug("sse_non_json_data", data=data_str[:100])
                            continue

                        try:
                            data = json.loads(data_str)
                            await self._handle_message(data)
                        except json.JSONDecodeError as e:
                            logger.warning("invalid_json", error=str(e), data=data_str[:100])

                except Exception as e:
                    logger.error("sse_processing_error", error=str(e))

    async def _handle_message(self, message: Dict[str, Any]):
        """
        Handle incoming JSON-RPC message from SSE stream.
        """
        # Check if it's a JSON-RPC response
        if "jsonrpc" in message and message.get("jsonrpc") == "2.0":
            request_id = message.get("id")

            if request_id and request_id in self._pending_requests:
                future = self._pending_requests.pop(request_id)

                if "error" in message:
                    error = message["error"]
                    future.set_exception(
                        MCPError(f"{error.get('message', 'Unknown error')} (code: {error.get('code')})")
                    )
                elif "result" in message:
                    future.set_result(message["result"])
                else:
                    future.set_exception(MCPError("Invalid JSON-RPC response"))
            else:
                logger.debug("unhandled_response", id=request_id)
        else:
            logger.debug("non_jsonrpc_message", message=message)

    async def _send_request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """
        Send JSON-RPC request over SSE and wait for response.

        Args:
            method: JSON-RPC method name
            params: Method parameters

        Returns:
            Method result
        """
        request_id = self._next_request_id()

        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {}
        }

        # Create future for response
        future = asyncio.Future()
        self._pending_requests[request_id] = future

        try:
            # Send request via POST to SSE endpoint
            # (MCP servers typically accept JSON-RPC via POST to same endpoint)
            headers = {
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json"
            }

            url = f"{self.url}/mcp/sse"
            timeout = aiohttp.ClientTimeout(total=self.timeout)

            async with self.session.post(url, json=request, headers=headers, timeout=timeout) as response:
                if response.status == 200:
                    # For some MCP servers, response comes immediately
                    result = await response.json()
                    if "result" in result:
                        return result["result"]
                    elif "error" in result:
                        raise MCPError(f"{result['error'].get('message', 'Unknown error')}")

            # Wait for response via SSE (with timeout)
            return await asyncio.wait_for(future, timeout=self.timeout)

        except asyncio.TimeoutError:
            self._pending_requests.pop(request_id, None)
            raise MCPError(f"Request timeout for method: {method}")
        except Exception as e:
            self._pending_requests.pop(request_id, None)
            raise

    async def discover_tools(self, force_refresh: bool = False):
        """
        Discover available MCP tools via JSON-RPC tools/list method.

        Args:
            force_refresh: Force refresh even if cache is valid
        """
        # Use cached tools if available and fresh (< 5 minutes old)
        if not force_refresh and self.tools and self.tools_last_updated:
            age = datetime.now() - self.tools_last_updated
            if age < timedelta(minutes=5):
                logger.debug("using_cached_tools", count=len(self.tools))
                return

        try:
            logger.debug("discovering_tools_via_jsonrpc")

            # Send tools/list JSON-RPC request
            result = await self._send_request("tools/list", {})

            # Parse tools from result
            self.tools = {}
            tools_list = result.get("tools", [])

            for tool in tools_list:
                tool_name = tool.get("name")
                if not tool_name:
                    continue

                # Determine if read-only based on name pattern
                is_read_only = self._is_read_only_tool(tool_name)

                self.tools[tool_name] = {
                    "description": tool.get("description", ""),
                    "input_schema": tool.get("inputSchema", {}),
                    "read_only": is_read_only,
                    "tags": tool.get("tags", [])
                }

            self.tools_last_updated = datetime.now()
            logger.info("tools_discovered", count=len(self.tools))

        except Exception as e:
            logger.error("tool_discovery_failed", error=str(e))
            # Don't raise - allow operation with empty tool list

    def _is_read_only_tool(self, tool_name: str) -> bool:
        """
        Determine if a tool is read-only based on naming patterns.
        """
        tool_lower = tool_name.lower()

        # Read-only patterns
        read_only_patterns = [
            'get', 'list', 'show', 'describe', 'query',
            'search', 'find', 'read', 'view', 'fetch'
        ]

        # Write operation patterns
        write_patterns = [
            'create', 'update', 'delete', 'remove', 'modify',
            'set', 'add', 'put', 'post', 'patch', 'configure'
        ]

        # Check for write patterns first (more specific)
        if any(pattern in tool_lower for pattern in write_patterns):
            return False

        # Check for read patterns
        if any(pattern in tool_lower for pattern in read_only_patterns):
            return True

        # Default to read-only for safety
        return True

    def get_tools(self, read_only: bool = True) -> List[Dict[str, Any]]:
        """
        Get list of available tools in LangChain/OpenAI function format.

        Args:
            read_only: If True, only return read-only (GET) operations

        Returns:
            List of tool definitions compatible with LLM tool calling
        """
        tools = []
        for name, info in self.tools.items():
            if read_only and not info.get("read_only", False):
                continue

            tool = {
                "type": "function",
                "function": {
                    "name": name,
                    "description": info.get("description", ""),
                    "parameters": self._convert_schema(info.get("input_schema", {}))
                }
            }
            tools.append(tool)

        return tools

    def _convert_schema(self, schema: Dict[str, Any]) -> Dict[str, Any]:
        """
        Convert MCP input schema to OpenAI function parameters format.
        """
        if not schema:
            return {"type": "object", "properties": {}}

        # MCP uses JSON Schema, which is compatible with OpenAI format
        # Just ensure it has the required structure
        if "type" not in schema:
            schema["type"] = "object"
        if "properties" not in schema:
            schema["properties"] = {}

        return schema

    async def call_tool(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        timeout: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Execute an MCP tool via JSON-RPC tools/call method.

        Args:
            tool_name: Name of the tool to execute
            arguments: Tool arguments as dictionary
            timeout: Optional timeout override

        Returns:
            Tool execution result

        Raises:
            ValueError: If tool not found
            MCPError: If tool execution fails
        """
        if tool_name not in self.tools:
            raise ValueError(
                f"Tool '{tool_name}' not found. "
                f"Available: {list(self.tools.keys())[:5]}..."
            )

        tool_info = self.tools[tool_name]
        logger.debug("calling_tool", tool=tool_name, args=arguments)

        try:
            # Send tools/call JSON-RPC request
            params = {
                "name": tool_name,
                "arguments": arguments
            }

            # Override timeout if provided
            original_timeout = self.timeout
            if timeout:
                self.timeout = timeout

            try:
                result = await self._send_request("tools/call", params)
            finally:
                self.timeout = original_timeout

            logger.info("tool_executed", tool=tool_name, success=True)

            return result

        except MCPError as e:
            logger.error("tool_execution_failed", tool=tool_name, error=str(e))
            raise
        except Exception as e:
            logger.error("tool_execution_error", tool=tool_name, error=str(e))
            raise MCPError(f"Tool execution failed: {str(e)}")

    async def call_tool_batch(
        self,
        calls: List[Dict[str, Any]],
        max_concurrent: int = 5
    ) -> List[Dict[str, Any]]:
        """
        Execute multiple MCP tools concurrently.

        Args:
            calls: List of {"tool": name, "arguments": {}} dictionaries
            max_concurrent: Maximum number of concurrent requests

        Returns:
            List of results in same order as input
        """
        semaphore = asyncio.Semaphore(max_concurrent)

        async def execute_with_limit(call):
            async with semaphore:
                try:
                    result = await self.call_tool(call["tool"], call["arguments"])
                    return {"success": True, "result": result}
                except Exception as e:
                    return {"success": False, "error": str(e), "tool": call["tool"]}

        tasks = [execute_with_limit(call) for call in calls]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        return results

    def get_read_only_tools(self) -> List[str]:
        """
        Get list of read-only tool names.
        """
        return [
            name for name, info in self.tools.items()
            if info.get("read_only", False)
        ]

    def get_tool_categories(self) -> Dict[str, List[str]]:
        """
        Categorize tools by API prefix.
        """
        categories = {
            "insights": [],
            "manage": [],
            "infrastructure": [],
            "onemanage": [],
            "other": []
        }

        for tool_name in self.tools:
            lower_name = tool_name.lower()
            if "insight" in lower_name:
                categories["insights"].append(tool_name)
            elif "manage" in lower_name or "policy" in lower_name:
                categories["manage"].append(tool_name)
            elif "infrastructure" in lower_name or "node" in lower_name:
                categories["infrastructure"].append(tool_name)
            elif "onemanage" in lower_name or "device" in lower_name:
                categories["onemanage"].append(tool_name)
            else:
                categories["other"].append(tool_name)

        return categories


# Global MCP client instance
_mcp_client: Optional[MCPClient] = None


def get_mcp_client() -> Optional[MCPClient]:
    """
    Get the global MCP client instance.
    """
    return _mcp_client


async def init_mcp_client(
    url: Optional[str] = None,
    token: Optional[str] = None
) -> MCPClient:
    """
    Initialize the global MCP client.

    Args:
        url: MCP server URL (defaults to MCP_SERVER_URL env var)
        token: Auth token (defaults to MCP_TOKEN env var)

    Returns:
        Initialized MCP client
    """
    global _mcp_client

    url = url or os.getenv("MCP_SERVER_URL")
    token = token or os.getenv("MCP_TOKEN")

    if not url or not token:
        raise ValueError("MCP_SERVER_URL and MCP_TOKEN environment variables required")

    _mcp_client = MCPClient(url=url, token=token)
    await _mcp_client.connect()

    return _mcp_client


async def shutdown_mcp_client():
    """
    Shutdown the global MCP client.
    """
    global _mcp_client
    if _mcp_client:
        await _mcp_client.disconnect()
        _mcp_client = None
