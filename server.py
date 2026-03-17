"""MCP server that bridges to a Symbolics Genera Lisp machine via TCP."""

import asyncio
import os
from mcp.server.fastmcp import FastMCP

GENERA_HOST = os.environ.get("GENERA_HOST", "genera")
GENERA_PORT = int(os.environ.get("GENERA_PORT", "8888"))
GENERA_TIMEOUT = int(os.environ.get("GENERA_TIMEOUT", "30"))

mcp = FastMCP("symbolics-genera")


async def read_field(reader: asyncio.StreamReader) -> str:
    """Read a length (output by Genera's PRINT: newline, digits, space), then that many bytes."""
    # Skip whitespace/newlines until we see a digit
    buf = b""
    while True:
        ch = await reader.readexactly(1)
        if ch.isdigit():
            buf += ch
            break
    # Read remaining digits until space
    while True:
        ch = await reader.readexactly(1)
        if ch.isdigit():
            buf += ch
        else:
            break  # the trailing space after the number
    length = int(buf)
    data = await reader.readexactly(length) if length > 0 else b""
    return data.decode("ascii", errors="replace").replace("\r\n", "\n").replace("\r", "\n")


async def genera_send(expression: str, host: str = GENERA_HOST, port: int = GENERA_PORT) -> tuple[str, str, str]:
    """Send a Lisp expression to Genera and return (result, stdout, stderr)."""
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port), GENERA_TIMEOUT)
    try:
        data = expression.encode("ascii")
        writer.write(f"{len(data)} ".encode("ascii") + data)
        await writer.drain()

        result = await asyncio.wait_for(read_field(reader), GENERA_TIMEOUT)
        stdout = await asyncio.wait_for(read_field(reader), GENERA_TIMEOUT)
        stderr = await asyncio.wait_for(read_field(reader), GENERA_TIMEOUT)
        return result, stdout, stderr
    finally:
        writer.close()
        await writer.wait_closed()


def format_response(result: str, stdout: str, stderr: str) -> str:
    """Format the three response fields into a single string for the LLM."""
    parts = []
    if result:
        parts.append(result)
    if stdout:
        parts.append(f"[stdout]\n{stdout}")
    if stderr:
        parts.append(f"[stderr]\n{stderr}")
    return "\n\n".join(parts) if parts else "(no output)"


@mcp.tool()
async def eval_lisp(expression: str) -> str:
    """Evaluate a Lisp expression on a Symbolics Genera machine and return the result.

    Args:
        expression: A Common Lisp / Zetalisp expression to evaluate.
    """
    try:
        result, stdout, stderr = await genera_send(expression)
        return format_response(result, stdout, stderr)
    except asyncio.TimeoutError:
        return f"Timeout: Genera did not respond within {GENERA_TIMEOUT}s"
    except Exception as e:
        return f"Connection error: {e}"


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
