"""python -m hdr.mcp_server  ->  Streamable HTTP MCP server on 127.0.0.1:8765/mcp"""

import os

import uvicorn

from hdr.mcp_server.app import app_from_env


def main() -> None:
    host = os.environ.get("HDR_MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("HDR_MCP_PORT", "8765"))
    uvicorn.run(app_from_env(), host=host, port=port, log_level=os.environ.get("HDR_LOG_LEVEL", "info"))


if __name__ == "__main__":
    main()
