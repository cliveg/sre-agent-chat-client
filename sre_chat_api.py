"""Ask Azure SRE Agent a question directly through its REST API."""

import argparse
import asyncio
import getpass
import json
import sys
import time
from urllib.parse import quote, urljoin, urlsplit

import httpx
from azure.core.credentials import AccessToken
from azure.core.exceptions import ClientAuthenticationError
from azure.identity.aio import AzureCliCredential

from sre_chat_common import (
    ClientError, argument_parser, completion_request, final_answer,
    positive_seconds, print_result, thread_uuid,
)


ARM = "https://management.azure.com"
ARM_SCOPE = ARM + "/.default"
AGENT_SCOPE = "https://azuresre.dev/.default"
API_VERSION = "2025-05-01-preview"


def validate_endpoint(value: object) -> str:
    if not isinstance(value, str):
        raise ClientError("Agent properties are missing agentEndpoint.")
    endpoint = urlsplit(value)
    if (
        endpoint.scheme != "https"
        or not endpoint.hostname
        or not endpoint.hostname.endswith(".azuresre.ai")
        or endpoint.username is not None
        or endpoint.password is not None
        or endpoint.port not in (None, 443)
        or endpoint.path not in ("", "/")
        or endpoint.query
        or endpoint.fragment
    ):
        raise ClientError("Expected an HTTPS agent endpoint under *.azuresre.ai.")
    return value.rstrip("/")


class SreApi:
    def __init__(self, http: httpx.AsyncClient, credential: AzureCliCredential):
        self.http = http
        self.credential = credential
        self.tokens: dict[str, AccessToken] = {}

    async def request(
        self, method: str, url: str, scope: str,
        body: dict[str, object] | None = None,
    ) -> object:
        token = self.tokens.get(scope)
        if token is None or token.expires_on <= time.time() + 60:
            token = await self.credential.get_token(scope)
            self.tokens[scope] = token
        response = await self.http.request(
            method, url,
            headers={"Authorization": f"Bearer {token.token}", "Accept": "application/json"},
            json=body,
        )
        # No redirects or retries: never forward credentials or duplicate a send.
        response.raise_for_status()
        if response.status_code == 204:
            return None
        try:
            return response.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ClientError(f"{method} {url} returned invalid JSON.") from exc

    async def endpoint(self, subscription: str, resource_group: str, agent: str) -> str:
        url = (
            f"{ARM}/subscriptions/{quote(subscription, safe='')}"
            f"/resourceGroups/{quote(resource_group, safe='')}"
            f"/providers/Microsoft.App/agents/{quote(agent, safe='')}"
            f"?api-version={API_VERSION}"
        )
        resource = await self.request("GET", url, ARM_SCOPE)
        properties = resource.get("properties") if isinstance(resource, dict) else None
        if not isinstance(properties, dict):
            raise ClientError("ARM response is missing agent properties.")
        return validate_endpoint(properties.get("agentEndpoint"))

    async def messages(self, endpoint: str, thread_id: str) -> list[object]:
        base = f"{endpoint}/api/v1/threads/{thread_id}/messages"
        url = base
        seen: set[str] = set()
        messages: list[object] = []
        while True:
            if url in seen:
                raise ClientError("The API returned a repeated message pagination link.")
            seen.add(url)
            payload = await self.request("GET", url, AGENT_SCOPE)
            if isinstance(payload, list):
                messages.extend(payload)
                return messages
            if not isinstance(payload, dict) or not isinstance(payload.get("value"), list):
                raise ClientError("Expected a messages array or an object with a value array.")
            messages.extend(payload["value"])
            next_link = payload.get("nextLink")
            if next_link is None or next_link == "":
                return messages
            if not isinstance(next_link, str):
                raise ClientError("Invalid message pagination link.")
            url = urljoin(url, next_link)
            parsed, expected = urlsplit(url), urlsplit(base)
            if (
                (parsed.scheme, parsed.netloc, parsed.path)
                != (expected.scheme, expected.netloc, expected.path)
                or parsed.fragment
            ):
                raise ClientError("Message pagination must stay on the same thread endpoint.")


async def ask(api: SreApi, args: argparse.Namespace) -> dict[str, str]:
    thread_id = args.thread_id
    if thread_id:
        print(f"Thread ID: {thread_id}", file=sys.stderr, flush=True)
    endpoint = await api.endpoint(args.subscription, args.resource_group, args.agent)
    text, marker = completion_request(args.question)
    user = getpass.getuser()
    message: dict[str, object] = {"text": text, "userId": user, "displayName": user}
    if thread_id:
        await api.request(
            "POST", f"{endpoint}/api/v1/threads/{thread_id}/messages", AGENT_SCOPE, message
        )
    else:
        created = await api.request(
            "POST", f"{endpoint}/api/v1/threads", AGENT_SCOPE,
            {"startMessage": {**message, "agent": args.agent}},
        )
        returned_id = created.get("id") if isinstance(created, dict) else None
        if not isinstance(returned_id, str):
            raise ClientError("Create-thread response is missing its id.")
        try:
            thread_id = thread_uuid(returned_id)
        except argparse.ArgumentTypeError as exc:
            raise ClientError("Create-thread response contains an invalid thread ID.") from exc
        print(f"Thread ID: {thread_id}", file=sys.stderr, flush=True)

    while True:
        messages = await api.messages(endpoint, thread_id)
        answer = final_answer({"messages": messages}, marker)
        if answer is not None:
            return {"thread_id": thread_id, "answer": answer}
        print("Waiting for the agent's final answer...", file=sys.stderr, flush=True)
        await asyncio.sleep(args.poll_interval)


async def run(args: argparse.Namespace) -> dict[str, str]:
    async with asyncio.timeout(args.timeout):
        async with AzureCliCredential(
            subscription=args.subscription, tenant_id=args.tenant or "", process_timeout=30
        ) as credential:
            async with httpx.AsyncClient(timeout=30, follow_redirects=False) as http:
                return await ask(SreApi(http, credential), args)


def parse_args() -> argparse.Namespace:
    parser = argument_parser(__doc__)
    parser.add_argument(
        "--timeout", type=positive_seconds, default=300,
        help="Overall authentication, endpoint lookup, send and polling timeout (default: 300).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nCancelled locally; the remote agent may still be running.", file=sys.stderr)
        return 130
    except TimeoutError:
        print(
            f"Timed out after {args.timeout:g} seconds without a confirmed final answer. "
            "Use any thread ID printed above for follow-up; the agent may still be running.",
            file=sys.stderr,
        )
        return 1
    except (ClientError, ClientAuthenticationError, httpx.HTTPError, OSError, ValueError) as exc:
        print(
            f"Request failed: {exc}\n"
            "Check az login and agent permissions for authentication errors. "
            "No send was retried. A request may have succeeded remotely; use any "
            "thread ID printed above for follow-up. Local failure does not cancel the agent.",
            file=sys.stderr,
        )
        return 1
    print_result(result, args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
