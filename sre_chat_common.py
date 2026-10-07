"""Transport-independent arguments and response handling for SRE chat clients."""

import argparse
import json
import math
from uuid import UUID, uuid4


class ClientError(Exception):
    """A response could not satisfy the conversation request."""


def completion_request(question: str) -> tuple[str, str]:
    marker = f"[SRE_CLIENT_DONE_{uuid4().hex}]"
    message = (
        question
        + "\n\nClient response-format instruction: Put your entire final answer "
        "in one message. End that message with the following exact marker on its "
        "own line. Do not include the marker in progress updates, tool calls, or "
        f"quoted examples:\n{marker}"
    )
    return message, marker


def final_answer(results: dict[str, object], marker: str) -> str | None:
    messages = results.get("messages")
    if not isinstance(messages, list):
        raise ClientError("Thread response is missing its messages array.")
    for message in reversed(messages):
        if not isinstance(message, dict):
            raise ClientError("Thread response contains an invalid message.")
        author = message.get("author")
        text = message.get("text")
        if (
            isinstance(author, dict)
            and author.get("role") in ("SREAgent", "assistant")
            and message.get("isComplete") is True
            and isinstance(text, str)
            and text.rstrip().endswith(marker)
        ):
            answer = text.rstrip()[: -len(marker)].strip()
            if not answer:
                raise ClientError("The agent completed the turn without an answer.")
            return answer
    return None


def positive_seconds(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Expected a positive number of seconds.") from exc
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("Seconds must be finite and greater than zero.")
    return number


def thread_uuid(value: str) -> str:
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Thread ID must be a UUID.") from exc


def nonempty(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("Value must not be empty.")
    return value.strip()


def argument_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("question", type=nonempty, help="Question to send to the SRE agent.")
    parser.add_argument("--thread-id", type=thread_uuid, help="Continue this conversation.")
    parser.add_argument(
        "--agent", type=nonempty, required=True, help="Your SRE agent resource name."
    )
    parser.add_argument(
        "--subscription", type=nonempty, required=True, help="Your Azure subscription ID."
    )
    parser.add_argument(
        "--resource-group", type=nonempty, required=True,
        help="Resource group containing your SRE agent.",
    )
    parser.add_argument("--tenant", type=nonempty, help="Optional Azure tenant ID.")
    parser.add_argument(
        "--poll-interval", type=positive_seconds, default=5,
        help="Seconds between thread reads (default: 5).",
    )
    parser.add_argument("--json", action="store_true", help="Write a JSON result to stdout.")
    return parser


def print_result(result: dict[str, str], as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=True))
    else:
        print(f"Thread ID: {result['thread_id']}\n\n{result['answer']}")
