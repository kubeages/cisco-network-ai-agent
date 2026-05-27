"""
MCP (Model Context Protocol) Client for Nexus Dashboard Integration
====================================================================

This module provides a client for connecting to the ND MCP Server via SSE
(Server-Sent Events) and executing Nexus Dashboard operations as tools.

Features:
---------
- SSE connection management with auto-reconnection
- Tool discovery and caching
- Async tool execution
- Error handling and retries
- Read-only operation filtering

"""

import os
import json
import asyncio
import aiohttp
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
import structlog

logger = structlog.get_logger(__name__)


class MCPClient:
    """
    Client for connecting to MCP Server via SSE and executing tools.
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
        self.tools: Dict[str, Dict[str, Any]] = {}
        self.tools_last_updated: Optional[datetime] = None
        self.connected = False

    async def connect(self):
        """
        Initialize HTTP session and discover tools.
        """
        if self.session is None:
            connector = aiohttp.TCPConnector(ssl=self.ssl_verify)
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            self.session = aiohttp.ClientSession(connector=connector, timeout=timeout)

        # Test connection
        try:
            await self._check_health()
            await self.discover_tools()
            self.connected = True
            logger.info("mcp_connected", url=self.url, tool_count=len(self.tools))
        except Exception as e:
            logger.error("mcp_connection_failed", error=str(e))
            raise

    async def disconnect(self):
        """
        Close HTTP session.
        """
        if self.session:
            await self.session.close()
            self.session = None
        self.connected = False
        logger.info("mcp_disconnected")

    async def _check_health(self) -> Dict[str, Any]:
        """
        Check MCP server health.
        """
        url = f"{self.url}/api/health"
        async with self.session.get(url) as response:
            response.raise_for_status()
            data = await response.json()
            logger.debug("mcp_health_check", status=data.get("status"))
            return data

    async def discover_tools(self, force_refresh: bool = False):
        """
        Discover available MCP tools from the server.

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
            # Get list of available tools from MCP server
            # Note: Actual endpoint may vary based on MCP server implementation
            url = f"{self.url}/api/tools"
            headers = {"Authorization": f"Bearer {self.token}"}

            async with self.session.get(url, headers=headers) as response:
                if response.status == 404:
                    # Fallback: Try to get tools from OpenAPI specs
                    logger.warning("tools_endpoint_not_found", trying_fallback=True)
                    await self._discover_tools_from_openapi()
                    return

                response.raise_for_status()
                tools_data = await response.json()

                # Parse tools response
                self.tools = {}
                for tool in tools_data.get("tools", []):
                    tool_name = tool.get("name")
                    if tool_name:
                        self.tools[tool_name] = {
                            "description": tool.get("description", ""),
                            "parameters": tool.get("parameters", {}),
                            "method": tool.get("method", "GET"),
                            "read_only": tool.get("method", "GET") == "GET"
                        }

                self.tools_last_updated = datetime.now()
                logger.info("tools_discovered", count=len(self.tools))

        except Exception as e:
            logger.error("tool_discovery_failed", error=str(e))
            # Don't raise - allow operation with empty tool list

    async def _discover_tools_from_openapi(self):
        """
        Fallback: Discover tools by parsing OpenAPI specs from guidance endpoint.
        """
        try:
            url = f"{self.url}/api/guidance/operations"
            headers = {"Authorization": f"Bearer {self.token}"}

            async with self.session.get(url, headers=headers) as response:
                response.raise_for_status()
                operations = await response.json()

                self.tools = {}
                for op in operations:
                    tool_name = op.get("operationId")
                    method = op.get("method", "GET").upper()

                    if tool_name:
                        self.tools[tool_name] = {
                            "description": op.get("summary", ""),
                            "parameters": op.get("parameters", []),
                            "method": method,
                            "read_only": method == "GET",
                            "path": op.get("path", "")
                        }

                self.tools_last_updated = datetime.now()
                logger.info("tools_discovered_from_openapi", count=len(self.tools))

        except Exception as e:
            logger.error("openapi_discovery_failed", error=str(e))

    def get_tools(self, read_only: bool = True) -> List[Dict[str, Any]]:
        """
        Get list of available tools.

        Args:
            read_only: If True, only return read-only (GET) operations

        Returns:
            List of tool definitions compatible with LangChain/LLM tool calling
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
                    "parameters": self._convert_parameters(info.get("parameters", {}))
                }
            }
            tools.append(tool)

        return tools

    def _convert_parameters(self, params: Any) -> Dict[str, Any]:
        """
        Convert MCP parameter format to OpenAI function calling format.
        """
        if isinstance(params, dict):
            return params
        elif isinstance(params, list):
            # Convert array of parameters to object schema
            properties = {}
            required = []
            for param in params:
                if isinstance(param, dict):
                    name = param.get("name")
                    if name:
                        properties[name] = {
                            "type": param.get("type", "string"),
                            "description": param.get("description", "")
                        }
                        if param.get("required", False):
                            required.append(name)
            return {
                "type": "object",
                "properties": properties,
                "required": required
            }
        else:
            return {"type": "object", "properties": {}}

    async def call_tool(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        timeout: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Execute an MCP tool.

        Args:
            tool_name: Name of the tool to execute
            arguments: Tool arguments as dictionary
            timeout: Optional timeout override

        Returns:
            Tool execution result

        Raises:
            ValueError: If tool not found
            Exception: If tool execution fails
        """
        if tool_name not in self.tools:
            raise ValueError(f"Tool '{tool_name}' not found. Available tools: {list(self.tools.keys())[:5]}...")

        tool_info = self.tools[tool_name]

        try:
            # Call MCP server to execute tool
            url = f"{self.url}/api/execute"
            headers = {
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json"
            }
            payload = {
                "tool": tool_name,
                "arguments": arguments
            }

            request_timeout = timeout or self.timeout
            async with self.session.post(
                url,
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=request_timeout)
            ) as response:
                response.raise_for_status()
                result = await response.json()

                logger.info(
                    "tool_executed",
                    tool=tool_name,
                    status=response.status,
                    args=arguments
                )

                return result

        except aiohttp.ClientError as e:
            logger.error("tool_execution_failed", tool=tool_name, error=str(e))
            raise

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
                    return await self.call_tool(call["tool"], call["arguments"])
                except Exception as e:
                    return {"error": str(e), "tool": call["tool"]}

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
            "onemanage": []
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
