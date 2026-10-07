import argparse
import asyncio
import contextlib
import io
import json
import time
import unittest
from unittest.mock import AsyncMock

import httpx
from azure.core.credentials import AccessToken
from azure.core.exceptions import ClientAuthenticationError
from azure.identity.aio import AzureCliCredential

from sre_chat_api import (
    AGENT_SCOPE, API_VERSION, ARM_SCOPE, ClientError, SreApi, ask, validate_endpoint,
)


ENDPOINT = "https://test-agent.region.azuresre.ai"
THREAD = "00000000-0000-4000-8000-000000000001"
MESSAGES_PATH = f"/api/v1/threads/{THREAD}/messages"


def options(thread_id=None):
    return argparse.Namespace(
        question="What do you know?", thread_id=thread_id, agent="test-agent",
        subscription="test-subscription", resource_group="test-rg", poll_interval=0.001,
    )


def credential():
    mock = AsyncMock(spec=AzureCliCredential)
    mock.get_token.return_value = AccessToken("test-token", int(time.time()) + 3600)
    return mock


def message(text, complete=True, role="SREAgent"):
    return {"author": {"role": role}, "text": text, "isComplete": complete}


class EndpointTests(unittest.TestCase):
    def test_endpoint_validation(self):
        self.assertEqual(validate_endpoint(ENDPOINT + "/"), ENDPOINT)
        for value in (
            None, "http://test.azuresre.ai", "https://example.com",
            "https://azuresre.ai.example.com", "https://user@test.azuresre.ai",
            "https://test.azuresre.ai:444", ENDPOINT + "/wrong-path",
            ENDPOINT + "?token=bad", ENDPOINT + "#fragment",
        ):
            with self.subTest(value=value), self.assertRaises(ClientError):
                validate_endpoint(value)


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_and_followup_wire_contract_and_polling(self):
        for existing in (None, THREAD):
            with self.subTest(existing=existing):
                marker = ""
                polls = 0
                posts = 0

                def handler(request):
                    nonlocal marker, polls, posts
                    self.assertEqual(request.headers["Authorization"], "Bearer test-token")
                    if request.url.host == "management.azure.com":
                        self.assertEqual(request.method, "GET")
                        self.assertEqual(request.url.params["api-version"], API_VERSION)
                        self.assertIn("/resourceGroups/test-rg/", request.url.path)
                        return httpx.Response(
                            200, json={"properties": {"agentEndpoint": ENDPOINT}}
                        )
                    if request.method == "POST":
                        posts += 1
                        payload = json.loads(request.content)
                        if existing:
                            self.assertEqual(request.url.path, MESSAGES_PATH)
                            self.assertNotIn("startMessage", payload)
                            self.assertNotIn("agent", payload)
                        else:
                            self.assertEqual(request.url.path, "/api/v1/threads")
                            payload = payload["startMessage"]
                            self.assertEqual(payload["agent"], "test-agent")
                        self.assertTrue(payload["userId"])
                        self.assertEqual(payload["userId"], payload["displayName"])
                        self.assertIn("What do you know?", payload["text"])
                        marker = payload["text"].splitlines()[-1]
                        return httpx.Response(200, json={"id": THREAD})
                    self.assertEqual(request.method, "GET")
                    self.assertEqual(request.url.path, MESSAGES_PATH)
                    polls += 1
                    messages = [
                        message("Old answer\n[SRE_CLIENT_DONE_previous]"),
                        message("Working on it."),
                        message(f"User echo\n{marker}", role="User"),
                        message(f"New answer\n{marker}", complete=polls >= 2),
                    ]
                    # Both response formats occur in the official client contract.
                    return httpx.Response(
                        200, json=messages if polls == 1 else {"value": messages}
                    )

                auth = credential()
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                    with contextlib.redirect_stderr(io.StringIO()):
                        result = await ask(SreApi(http, auth), options(existing))
                self.assertEqual(result, {"thread_id": THREAD, "answer": "New answer"})
                self.assertEqual(polls, 2)
                self.assertEqual(posts, 1)
                self.assertEqual(
                    [call.args[0] for call in auth.get_token.await_args_list],
                    [ARM_SCOPE, AGENT_SCOPE],
                )

    async def test_missing_create_id_fails_without_resend(self):
        posts = 0

        def handler(request):
            nonlocal posts
            if request.method == "POST":
                posts += 1
                return httpx.Response(200, json={})
            return httpx.Response(200, json={"properties": {"agentEndpoint": ENDPOINT}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            with self.assertRaisesRegex(ClientError, "missing its id"):
                await ask(SreApi(http, credential()), options())
        self.assertEqual(posts, 1)

    async def test_authentication_failure_makes_no_http_request(self):
        auth = credential()
        auth.get_token.side_effect = ClientAuthenticationError("Sign in first")
        handler = AsyncMock()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            with self.assertRaises(ClientAuthenticationError):
                await SreApi(http, auth).request("GET", ENDPOINT, AGENT_SCOPE)
        handler.assert_not_called()

    async def test_refreshes_expiring_token(self):
        auth = credential()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[]))
        ) as http:
            api = SreApi(http, auth)
            api.tokens[AGENT_SCOPE] = AccessToken("expiring-token", int(time.time()) + 10)
            await api.request("GET", ENDPOINT, AGENT_SCOPE)
            await api.request("GET", ENDPOINT, AGENT_SCOPE)
        auth.get_token.assert_awaited_once_with(AGENT_SCOPE)

    async def test_http_errors_and_redirects_are_not_retried(self):
        for status in (302, 401, 403, 429, 500):
            handler = AsyncMock(return_value=httpx.Response(
                status, headers={"Location": "https://example.com"}, json={}
            ))
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                with self.subTest(status=status), self.assertRaises(httpx.HTTPStatusError):
                    await SreApi(http, credential()).request(
                        "POST", ENDPOINT, AGENT_SCOPE, {"text": "question"}
                    )
            self.assertEqual(handler.await_count, 1)

    async def test_invalid_json_and_message_shapes(self):
        responses = [
            httpx.Response(200, text="not JSON"),
            httpx.Response(200, json={"value": "not an array"}),
        ]
        for response in responses:
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(lambda _: response)
            ) as http:
                with self.subTest(response=response), self.assertRaises(ClientError):
                    await SreApi(http, credential()).messages(ENDPOINT, THREAD)

    async def test_message_pagination(self):
        def handler(request):
            if "page" not in request.url.params:
                return httpx.Response(
                    200, json={"value": [{"id": "first"}], "nextLink": "?page=2"}
                )
            return httpx.Response(200, json={"value": [{"id": "second"}], "nextLink": None})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            messages = await SreApi(http, credential()).messages(ENDPOINT, THREAD)
        self.assertEqual(messages, [{"id": "first"}, {"id": "second"}])

    async def test_rejects_unsafe_or_repeated_pagination(self):
        for link in (
            "https://example.com/messages",
            ENDPOINT + "/different-thread/messages",
            ENDPOINT + MESSAGES_PATH,
        ):
            handler = AsyncMock(return_value=httpx.Response(
                200, json={"value": [], "nextLink": link}
            ))
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                with self.subTest(link=link), self.assertRaises(ClientError):
                    await SreApi(http, credential()).messages(ENDPOINT, THREAD)
            self.assertEqual(handler.await_count, 1)

    async def test_timeout_does_not_resend(self):
        api = AsyncMock(spec=SreApi)
        api.endpoint.return_value = ENDPOINT
        api.request.return_value = {"id": THREAD}
        api.messages.return_value = [message("Still working.")]
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(TimeoutError):
            async with asyncio.timeout(0.02):
                await ask(api, options())
        api.request.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
