"""
AWS MCP Adapter - Encapsulates the AWS MCP Server connection via mcp-proxy-for-aws.

This adapter isolates the MCP SDK details from the rest of the application.
"""

from typing import Optional, List, Dict, Any
from datetime import datetime
from dataclasses import dataclass

from langchain_mcp_adapters.client import MultiServerMCPClient


@dataclass
class MCPToolResult:
    """Result from an MCP tool invocation."""
    tool_name: str
    result: Any
    success: bool
    error: Optional[str] = None


class AWSMCPAdapter:
    """
    Adapter for AWS MCP Server connection via mcp-proxy-for-aws (SigV4).
    
    This adapter:
    - Manages the MCP client connection via mcp-proxy-for-aws (SigV4 auth)
    - Provides a clean interface for tool invocation
    - Handles connection lifecycle
    - Converts MCP results to our Evidence format
    """
    
    def __init__(
        self,
        mcp_endpoint: str = "https://aws-mcp.us-east-1.api.aws/mcp",
        aws_region: str = "us-east-1",
        proxy_version: str = "latest",
        skip_auth: bool = True,
    ):
        self.mcp_endpoint = mcp_endpoint
        self.aws_region = aws_region
        self.proxy_version = proxy_version
        self.skip_auth = skip_auth
        
        self._client: Optional[MultiServerMCPClient] = None
        self._tools: Optional[List[Any]] = None
        self._connected = False
    
    @property
    def is_connected(self) -> bool:
        return self._connected and self._client is not None
    
    def _build_connection_config(self) -> Dict[str, Any]:
        """Build the MCP server configuration using mcp-proxy-for-aws with SigV4."""
        # Use mcp-proxy-for-aws with SigV4 authentication via stdio
        # Use --skip-auth for public tools (documentation search, etc.)
        # The endpoint must be the LAST positional argument
        args = [
            "--metadata", f"AWS_REGION={self.aws_region}",
            "--skip-auth",
            self.mcp_endpoint,
        ]
        
        return {
            "aws-mcp": {
                "command": "mcp-proxy-for-aws",
                "args": args,
                "transport": "stdio",
            }
        }
    
    async def connect(self) -> bool:
        """Establish connection to AWS MCP Server via mcp-proxy-for-aws."""
        try:
            config = self._build_connection_config()
            self._client = MultiServerMCPClient(config)
            self._tools = await self._client.get_tools()
            self._connected = True
            return True
        except Exception as e:
            self._connected = False
            raise ConnectionError(f"Failed to connect to AWS MCP Server: {e}")
    
    async def disconnect(self):
        """Close the MCP connection."""
        if self._client:
            try:
                await self._client.__aexit__(None, None, None)
            except Exception:
                pass
            self._client = None
            self._tools = None
            self._connected = False
    
    async def get_tools(self) -> List[Any]:
        """Get available MCP tools."""
        if not self._connected:
            await self.connect()
        return self._tools or []
    
    async def list_available_tools(self) -> List[Dict[str, str]]:
        """List available tool names and descriptions."""
        tools = await self.get_tools()
        return [
            {
                "name": tool.name,
                "description": getattr(tool, "description", ""),
            }
            for tool in tools
        ]
    
    async def search_documentation(
        self,
        query: str,
        topic: Optional[str] = None,
    ) -> MCPToolResult:
        """Search AWS documentation."""
        return await self._invoke_tool("aws___search_documentation", {
            "search_phrase": query,
            "topic": topic,
        })
    
    async def read_documentation(self, url: str) -> MCPToolResult:
        """Read specific AWS documentation page."""
        return await self._invoke_tool("aws___read_documentation", {
            "url": url,
        })
    
    async def retrieve_skill(self, skill_name: str) -> MCPToolResult:
        """Retrieve a specific AWS skill."""
        return await self._invoke_tool("aws___retrieve_skill", {
            "skill_name": skill_name,
        })
    
    async def list_skills(self) -> MCPToolResult:
        """List available AWS skills."""
        return await self._invoke_tool("aws___list_skills", {})
    
    async def list_regions(self) -> MCPToolResult:
        """List AWS regions."""
        return await self._invoke_tool("aws___list_regions", {})
    
    async def get_regional_availability(
        self,
        service: str,
        region: Optional[str] = None,
    ) -> MCPToolResult:
        """Check regional availability for a service."""
        return await self._invoke_tool("aws___get_regional_availability", {
            "service": service,
            "region": region,
        })
    
    async def search_skills(self, query: str) -> MCPToolResult:
        """Search for skills."""
        return await self._invoke_tool("aws___search_documentation", {
            "search_phrase": query,
            "topic": "skills",
        })
    
    async def retrieve_skill(self, skill_name: str) -> MCPToolResult:
        """Retrieve a specific skill."""
        return await self._invoke_tool("aws___retrieve_skill", {
            "skill_name": skill_name,
        })
    
    async def _invoke_tool(self, tool_name: str, arguments: Dict[str, Any]) -> MCPToolResult:
        """Invoke an MCP tool by name with arguments."""
        if not self._connected:
            await self.connect()
        
        # Find the tool
        tool = None
        for tool in self._tools:
            if tool.name == tool_name:
                tool = tool
                break
        
        if not tool:
            return MCPToolResult(
                tool_name=tool_name,
                result=None,
                success=False,
                error=f"Tool '{tool_name}' not found",
            )
        
        try:
            result = await tool.ainvoke(arguments)
            return MCPToolResult(
                tool_name=tool_name,
                result=result,
                success=True,
            )
        except Exception as e:
            return MCPToolResult(
                tool_name=tool_name,
                result=None,
                success=False,
                error=str(e),
            )
    
    async def search_and_read_documentation(
        self,
        query: str,
        max_results: int = 5,
    ) -> List[MCPToolResult]:
        """Search documentation and read top results."""
        search_result = await self.search_documentation(query)
        
        if not search_result.success:
            return [search_result]
        
        # Parse search results to get URLs
        # The search result format depends on the MCP server response
        # We'll try to extract URLs from the search results
        results = [search_result]
        
        # Try to extract URLs and read them
        # This is best-effort as the exact format varies
        return results