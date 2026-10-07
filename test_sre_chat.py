import argparse
import asyncio
import contextlib
import io
import json
import unittest
from unittest.mock import AsyncMock

from mcp import ClientSession, types

from sre_chat import (
    CREATE, GET, SEND, ClientError, ask, decode_result, final_answer,
    nonempty, positive_seconds, thread_uuid,
)
from sre_chat_common import argument_parser


THREAD = "00000000-0000-4000-8000-000000000001"
MARKER = "[SRE_CLIENT_DONE_test]"
TARGET = {"agent": "test-agent", "subscription": "test-sub", "resource-group": "test-rg"}


def message(text: str, *, role: str = "SREAgent", complete: bool = True) -> dict:
    return {"author": {"role": role}, "text": text, "isComplete": complete}


def response(messages: list, *, thread_id: str = THREAD) -> types.CallToolResult:
    payload = {
        "status": 200,
        "results": {"threadId": thread_id, "messages": messages},
    }
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload))]
    )


class ParsingTests(unittest.TestCase):
    def test_target_arguments_are_required(self):
        target = [
            ("--agent", "test-agent"),
            ("--subscription", "test-sub"),
            ("--resource-group", "test-rg"),
        ]
        parser = argument_parser("Test client")
        for missing, _ in target:
            arguments = ["Question"]
            for flag, value in target:
                if flag != missing:
                    arguments.extend([flag, value])
            with self.subTest(missing=missing), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    parser.parse_args(arguments)
                self.assertEqual(error.exception.code, 2)

    def test_explicit_target_arguments(self):
        args = argument_parser("Test client").parse_args([
            "Question", "--agent", "test-agent", "--subscription", "test-sub",
            "--resource-group", "test-rg",
        ])
        self.assertEqual(args.agent, TARGET["agent"])
        self.assertEqual(args.subscription, TARGET["subscription"])
        self.assertEqual(args.resource_group, TARGET["resource-group"])

    def test_text_and_structured_results(self):
        self.assertEqual(decode_result(response([]))["threadId"], THREAD)
        structured = types.CallToolResult(
            content=[],
            structuredContent={"status": 200, "results": {"threadId": THREAD}},
        )
        self.assertEqual(decode_result(structured)["threadId"], THREAD)

    def test_errors_are_not_successful_answers(self):
        cases = [
            types.CallToolResult(
                isError=True, content=[types.TextContent(type="text", text="Forbidden")]
            ),
            types.CallToolResult(
                content=[], structuredContent={"status": 403, "message": "Forbidden"}
            ),
            types.CallToolResult(content=[types.TextContent(type="text", text="bad JSON")]),
            types.CallToolResult(content=[], structuredContent={"status": 200}),
        ]
        for result in cases:
            with self.subTest(result=result), self.assertRaises(ClientError):
                decode_result(result)

    def test_only_completed_final_answer_matches(self):
        ignored = [
            message("Reading memory."),
            message(f"question\n{MARKER}", role="User"),
            message(f"tool result\n{MARKER}", role="Tool"),
            message(f"unfinished\n{MARKER}", complete=False),
            message(f"{MARKER}\nStill working"),
            message("Old answer\n[SRE_CLIENT_DONE_previous]"),
        ]
        self.assertIsNone(final_answer({"messages": ignored}, MARKER))
        ignored.append(message(f"The answer.\n{MARKER}\n"))
        self.assertEqual(final_answer({"messages": ignored}, MARKER), "The answer.")

    def test_malformed_and_empty_answers_fail(self):
        for results in ({}, {"messages": [42]}, {"messages": [message(MARKER)]}):
            with self.subTest(results=results), self.assertRaises(ClientError):
                final_answer(results, MARKER)

    def test_invalid_arguments(self):
        for value in ("0", "-1", "nan", "inf", "abc"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                positive_seconds(value)
        self.assertEqual(positive_seconds("0.5"), 0.5)
        with self.assertRaises(argparse.ArgumentTypeError):
            thread_uuid("invalid")
        with self.assertRaises(argparse.ArgumentTypeError):
            nonempty(" ")
        self.assertEqual(thread_uuid(THREAD), THREAD)


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    async def converse(self, session, thread_id=None, poll_interval=0.001):
        with contextlib.redirect_stderr(io.StringIO()):
            return await ask(
                session, question="What do you know?", target=TARGET,
                thread_id=thread_id, poll_interval=poll_interval,
            )

    async def test_new_and_followup_poll_correct_thread(self):
        for thread_id, command in ((None, CREATE), (THREAD, SEND)):
            with self.subTest(thread_id=thread_id):
                session = AsyncMock(spec=ClientSession)
                marker = ""

                async def call(name, arguments):
                    nonlocal marker
                    if name == command:
                        marker = arguments["message"].splitlines()[-1]
                        self.assertTrue(marker.startswith("[SRE_CLIENT_DONE_"))
                        self.assertEqual(arguments["agent"], TARGET["agent"])
                        if thread_id:
                            self.assertEqual(arguments["thread-id"], THREAD)
                        else:
                            self.assertNotIn("thread-id", arguments)
                        return response([message("I'll read my memories.")])
                    self.assertEqual(name, GET)
                    self.assertEqual(arguments["thread-id"], THREAD)
                    return response([message(f"Final answer\n{marker}")])

                session.call_tool.side_effect = call
                result = await self.converse(session, thread_id)
                self.assertEqual(result, {"thread_id": THREAD, "answer": "Final answer"})
                self.assertEqual(session.call_tool.await_count, 2)

    async def test_immediate_final_does_not_poll(self):
        session = AsyncMock(spec=ClientSession)

        async def call(_name, arguments):
            return response([message(f"Answer\n{arguments['message'].splitlines()[-1]}")])

        session.call_tool.side_effect = call
        self.assertEqual((await self.converse(session))["answer"], "Answer")
        session.call_tool.assert_awaited_once()

    async def test_timeout_does_not_resend(self):
        session = AsyncMock(spec=ClientSession)
        session.call_tool.return_value = response([message("Still working.")])
        with self.assertRaises(TimeoutError):
            async with asyncio.timeout(0.02):
                await self.converse(session)
        sends = [call for call in session.call_tool.call_args_list if call.args[0] == CREATE]
        self.assertEqual(len(sends), 1)

    async def test_unknown_or_mismatched_thread_fails(self):
        for value in ("", "different-thread"):
            session = AsyncMock(spec=ClientSession)
            session.call_tool.return_value = response([], thread_id=value)
            with self.subTest(value=value), self.assertRaises(ClientError):
                await self.converse(session, THREAD)
            session.call_tool.assert_awaited_once()

    async def test_poll_error_propagates(self):
        session = AsyncMock(spec=ClientSession)
        session.call_tool.side_effect = [
            response([]),
            types.CallToolResult(
                content=[], structuredContent={"status": 403, "message": "Forbidden"}
            ),
        ]
        with self.assertRaisesRegex(ClientError, "403"):
            await self.converse(session)


if __name__ == "__main__":
    unittest.main()
