"""Ask Azure SRE Agent a question through its stdio MCP server."""

import argparse
import asyncio
import json
import os
import shutil
import sys
import traceback

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError

from sre_chat_common import (
    ClientError, argument_parser, completion_request, final_answer,
    nonempty, positive_seconds, print_result, thread_uuid,
)

MCP_VERSION = "3.0.0-beta.49"
CREATE = "sreagent_threads_create"
SEND = "sreagent_threads_send_message"
GET = "sreagent_threads_get"


def decode_result(result: types.CallToolResult) -> dict[str, object]:
    text = "\n".join(
        item.text for item in result.content if isinstance(item, types.TextContent)
    )
    if result.isError:
        raise ClientError(f"MCP tool failed: {text or result.structuredContent}")
    payload: object = result.structuredContent
    if payload is None:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ClientError(f"MCP returned invalid JSON: {text[:500]}") from exc
    if not isinstance(payload, dict):
        raise ClientError("MCP response must be an object.")
    status = payload.get("status")
    if not isinstance(status, int) or not 200 <= status < 300:
        raise ClientError(
            f"Azure MCP request failed (status {status}): {payload.get('message')}"
        )
    results = payload.get("results")
    if not isinstance(results, dict):
        raise ClientError("Azure MCP response is missing its results object.")
    return results


async def ask(
    session: ClientSession,
    *,
    question: str,
    target: dict[str, str],
    thread_id: str | None,
    poll_interval: float,
) -> dict[str, str]:
    message, marker = completion_request(question)
    parameters = {**target, "message": message}
    if thread_id:
        parameters["thread-id"] = thread_id
        print(f"Thread ID: {thread_id}", file=sys.stderr, flush=True)
    # Never retry a send: a timed-out request may already have reached the agent.
    results = decode_result(
        await session.call_tool(SEND if thread_id else CREATE, parameters)
    )
    returned_id = results.get("threadId")
    if not isinstance(returned_id, str) or not returned_id:
        raise ClientError("The agent did not return a thread ID.")
    if thread_id and returned_id != thread_id:
        raise ClientError(f"Agent returned an unexpected thread ID: {returned_id}")
    if not thread_id:
        print(f"Thread ID: {returned_id}", file=sys.stderr, flush=True)
    thread_id = returned_id

    while True:
        answer = final_answer(results, marker)
        if answer is not None:
            return {"thread_id": thread_id, "answer": answer}
        print("Waiting for the agent's final answer...", file=sys.stderr, flush=True)
        await asyncio.sleep(poll_interval)
        results = decode_result(
            await session.call_tool(GET, {**target, "thread-id": thread_id})
        )
        if results.get("threadId") != thread_id:
            raise ClientError("Polling returned a different thread ID.")


async def run(args: argparse.Namespace) -> dict[str, str]:
    command = shutil.which("npx.cmd" if os.name == "nt" else "npx")
    if command is None:
        raise ClientError("npx was not found. Install Node.js and reopen your terminal.")
    server = StdioServerParameters(
        command=command,
        args=[
            "-y",
            f"@azure/mcp@{MCP_VERSION}",
            "server",
            "start",
            "--tool", CREATE,
            "--tool", SEND,
            "--tool", GET,
            "--disable-caching",
        ],
        env={**os.environ, "AZURE_TOKEN_CREDENTIALS": "AzureCliCredential"},
    )
    target = {
        "agent": args.agent,
        "subscription": args.subscription,
        "resource-group": args.resource_group,
    }
    if args.tenant:
        target["tenant"] = args.tenant
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            async with asyncio.timeout(args.timeout):
                await session.initialize()
                tools: set[str] = set()
                cursor = None
                while True:
                    page = await session.list_tools(
                        params=types.PaginatedRequestParams(cursor=cursor)
                    )
                    tools.update(tool.name for tool in page.tools)
                    cursor = page.nextCursor
                    if not cursor:
                        break
                missing = {CREATE, SEND, GET} - tools
                if missing:
                    raise ClientError(f"MCP server is missing tools: {sorted(missing)}")
                return await ask(
                    session,
                    question=args.question,
                    target=target,
                    thread_id=args.thread_id,
                    poll_interval=args.poll_interval,
                )


def parse_args() -> argparse.Namespace:
    parser = argument_parser(__doc__)
    parser.add_argument(
        "--timeout", type=positive_seconds, default=300,
        help="Timeout for MCP initialization, sending and polling in seconds (default: 300).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nCancelled locally; the remote agent may still be running.", file=sys.stderr)
        return 130
    except (ClientError, TimeoutError, OSError, McpError, ExceptionGroup) as exc:
        print(
            "Request failed. Any thread ID printed above can be used for follow-up. "
            "A send may have succeeded remotely; it was not retried. "
            "Local failure does not cancel the agent.",
            file=sys.stderr,
        )
        traceback.print_exception(exc)
        return 1
    print_result(result, args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
