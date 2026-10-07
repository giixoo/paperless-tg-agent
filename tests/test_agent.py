from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anthropic
import httpx
import pytest

from paperbot.agent.agent import AgentMemory, run_agent
from paperbot.budget import BudgetStore, PriceConfig
from paperbot.config import Settings
from paperbot.paperless import Document

TZ = "Europe/Warsaw"


# --- scripted Anthropic client -------------------------------------------------


@dataclass
class FakeTextBlock:
    text: str
    type: str = "text"


@dataclass
class FakeToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]
    type: str = "tool_use"


@dataclass
class FakeUsage:
    input_tokens: int = 10
    output_tokens: int = 5
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class FakeMessage:
    content: list[Any]
    stop_reason: str
    usage: FakeUsage = field(default_factory=FakeUsage)


class FakeMessagesResource:
    def __init__(self, responses: list[FakeMessage | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> FakeMessage:
        # Snapshot `messages` (mutated in place by the caller after this
        # call returns) so later assertions see the state as of this call.
        snapshot = dict(kwargs)
        if "messages" in snapshot:
            snapshot["messages"] = list(snapshot["messages"])
        self.calls.append(snapshot)
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeAnthropicClient:
    def __init__(self, responses: list[FakeMessage | Exception]) -> None:
        self.messages = FakeMessagesResource(responses)


# --- fake Paperless for tool dispatch ------------------------------------------


def make_doc(**overrides: Any) -> Document:
    defaults: dict[str, Any] = dict(
        id=412,
        title="Car insurance 2026",
        created=None,
        correspondent="PZU",
        document_type="Insurance policy",
        tags=["Insurance"],
        custom_fields={},
    )
    defaults.update(overrides)
    return Document(**defaults)


@dataclass
class FakePaperless:
    search_results: tuple[list[Document], int] = field(default_factory=lambda: ([], 0))
    detail: Document | None = None

    async def search_documents(self, query: str, **kwargs: Any) -> tuple[list[Document], int]:
        return self.search_results

    async def get_document(self, doc_id: int) -> Document | None:
        return self.detail


@pytest.fixture
def settings() -> Settings:
    return Settings(
        telegram_bot_token="t",  # noqa: S106
        telegram_allowed_users=[1],
        paperless_url="http://paperless.test",  # type: ignore[arg-type]
        paperless_token="pt",  # noqa: S106
        anthropic_api_key="ak",  # noqa: S106
        agent_max_steps=5,
        daily_budget_usd=1.0,
        price_input_per_mtok=1.0,
        price_output_per_mtok=5.0,
    )


def make_budget_store(tmp_path: Path) -> BudgetStore:
    return BudgetStore(
        tmp_path / "budget.sqlite3",
        PriceConfig(1.0, 5.0, 1.25, 0.10),
    )


async def noop_send_document(doc_id: int) -> bool:
    return True


# --- tests -----------------------------------------------------------------


async def test_end_turn_immediately_returns_text(settings: Settings, tmp_path: Path) -> None:
    client = FakeAnthropicClient(
        [FakeMessage(content=[FakeTextBlock(text="Hello there")], stop_reason="end_turn")]
    )
    budget = make_budget_store(tmp_path)
    memory = AgentMemory(settings.history_turns)

    result = await run_agent(
        chat_id=1,
        user_text="hi",
        lang="en",
        anthropic_client=client,  # type: ignore[arg-type]
        settings=settings,
        paperless=FakePaperless(),  # type: ignore[arg-type]
        budget_store=budget,
        memory=memory,
        send_document_callback=noop_send_document,
    )

    assert result.text == "Hello there"
    assert result.mentioned_docs == []
    assert len(client.messages.calls) == 1
    assert memory.history(1) == [("user", "hi"), ("assistant", "Hello there")]


async def test_tool_use_then_end_turn(settings: Settings, tmp_path: Path) -> None:
    client = FakeAnthropicClient(
        [
            FakeMessage(
                content=[
                    FakeToolUseBlock(id="t1", name="search_documents", input={"query": "insurance"})
                ],
                stop_reason="tool_use",
            ),
            FakeMessage(content=[FakeTextBlock(text="Found #412")], stop_reason="end_turn"),
        ]
    )
    budget = make_budget_store(tmp_path)
    memory = AgentMemory(settings.history_turns)
    paperless = FakePaperless(search_results=([make_doc()], 1))

    result = await run_agent(
        chat_id=1,
        user_text="find my insurance",
        lang="en",
        anthropic_client=client,  # type: ignore[arg-type]
        settings=settings,
        paperless=paperless,  # type: ignore[arg-type]
        budget_store=budget,
        memory=memory,
        send_document_callback=noop_send_document,
    )

    assert result.text == "Found #412"
    # the reply cites #412, and search_documents returned data for it this
    # turn - so it's offered as a mentioned_doc (-> download/preview buttons)
    assert result.mentioned_docs == [(412, "Car insurance 2026")]
    assert len(client.messages.calls) == 2

    # the second call's messages include the tool_result for t1
    second_call_messages = client.messages.calls[1]["messages"]
    tool_result_message = second_call_messages[-1]
    assert tool_result_message["role"] == "user"
    assert tool_result_message["content"][0]["tool_use_id"] == "t1"

    # recent_docs memory was updated from the tool result
    assert memory.recent_docs(1) == [(412, "Car insurance 2026")]


async def test_recent_docs_show_up_in_next_turns_system_prompt(
    settings: Settings, tmp_path: Path
) -> None:
    client = FakeAnthropicClient(
        [
            FakeMessage(
                content=[FakeToolUseBlock(id="t1", name="get_document", input={"id": 412})],
                stop_reason="tool_use",
            ),
            FakeMessage(content=[FakeTextBlock(text="ok")], stop_reason="end_turn"),
            FakeMessage(content=[FakeTextBlock(text="ok2")], stop_reason="end_turn"),
        ]
    )
    budget = make_budget_store(tmp_path)
    memory = AgentMemory(settings.history_turns)
    paperless = FakePaperless(detail=make_doc())

    await run_agent(
        chat_id=1,
        user_text="tell me about #412",
        lang="en",
        anthropic_client=client,  # type: ignore[arg-type]
        settings=settings,
        paperless=paperless,  # type: ignore[arg-type]
        budget_store=budget,
        memory=memory,
        send_document_callback=noop_send_document,
    )
    await run_agent(
        chat_id=1,
        user_text="anything else?",
        lang="en",
        anthropic_client=client,  # type: ignore[arg-type]
        settings=settings,
        paperless=paperless,  # type: ignore[arg-type]
        budget_store=budget,
        memory=memory,
        send_document_callback=noop_send_document,
    )

    third_call_system = client.messages.calls[2]["system"]
    assert "#412 Car insurance 2026" in third_call_system[0]["text"]

    third_call_messages = client.messages.calls[2]["messages"]
    assert {"role": "user", "content": "tell me about #412"} in third_call_messages
    assert {"role": "assistant", "content": "ok"} in third_call_messages


async def test_budget_reached_skips_api_call(settings: Settings, tmp_path: Path) -> None:
    client = FakeAnthropicClient([])  # must never be called
    budget = make_budget_store(tmp_path)
    await budget.record_usage(
        tz_name=settings.tz,
        input_tokens=2_000_000,  # $2.00 at $1/Mtok, over the $1.00 daily budget
        output_tokens=0,
        cache_write_tokens=0,
        cache_read_tokens=0,
    )
    memory = AgentMemory(settings.history_turns)

    result = await run_agent(
        chat_id=1,
        user_text="hi",
        lang="en",
        anthropic_client=client,  # type: ignore[arg-type]
        settings=settings,
        paperless=FakePaperless(),  # type: ignore[arg-type]
        budget_store=budget,
        memory=memory,
        send_document_callback=noop_send_document,
    )

    assert "budget" in result.text.lower()
    assert result.mentioned_docs == []
    assert client.messages.calls == []


async def test_max_steps_exhausted_forces_final_call_without_tools(
    settings: Settings, tmp_path: Path
) -> None:
    settings.agent_max_steps = 1
    client = FakeAnthropicClient(
        [
            FakeMessage(
                content=[FakeToolUseBlock(id="t1", name="search_documents", input={"query": "x"})],
                stop_reason="tool_use",
            ),
            FakeMessage(content=[FakeTextBlock(text="final answer")], stop_reason="end_turn"),
        ]
    )
    budget = make_budget_store(tmp_path)
    memory = AgentMemory(settings.history_turns)

    result = await run_agent(
        chat_id=1,
        user_text="hi",
        lang="en",
        anthropic_client=client,  # type: ignore[arg-type]
        settings=settings,
        paperless=FakePaperless(),  # type: ignore[arg-type]
        budget_store=budget,
        memory=memory,
        send_document_callback=noop_send_document,
    )

    assert result.text == "final answer"
    assert len(client.messages.calls) == 2
    assert "tools" not in client.messages.calls[1]


async def test_api_error_returns_friendly_message(settings: Settings, tmp_path: Path) -> None:
    error = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))
    client = FakeAnthropicClient([error])
    budget = make_budget_store(tmp_path)
    memory = AgentMemory(settings.history_turns)

    result = await run_agent(
        chat_id=1,
        user_text="hi",
        lang="en",
        anthropic_client=client,  # type: ignore[arg-type]
        settings=settings,
        paperless=FakePaperless(),  # type: ignore[arg-type]
        budget_store=budget,
        memory=memory,
        send_document_callback=noop_send_document,
    )

    assert "wrong" in result.text.lower() or "error" in result.text.lower()
    # a failed call must not be recorded into conversation memory
    assert memory.history(1) == []


async def test_budget_is_recorded_from_usage(settings: Settings, tmp_path: Path) -> None:
    client = FakeAnthropicClient(
        [
            FakeMessage(
                content=[FakeTextBlock(text="ok")],
                stop_reason="end_turn",
                usage=FakeUsage(
                    input_tokens=1000,
                    output_tokens=200,
                    cache_creation_input_tokens=50,
                    cache_read_input_tokens=0,
                ),
            )
        ]
    )
    budget = make_budget_store(tmp_path)
    memory = AgentMemory(settings.history_turns)

    await run_agent(
        chat_id=1,
        user_text="hi",
        lang="en",
        anthropic_client=client,  # type: ignore[arg-type]
        settings=settings,
        paperless=FakePaperless(),  # type: ignore[arg-type]
        budget_store=budget,
        memory=memory,
        send_document_callback=noop_send_document,
    )

    usage = await budget.today_usage(settings.tz)
    assert usage.input_tokens == 1000
    assert usage.output_tokens == 200
    assert usage.cache_write_tokens == 50


async def test_clear_resets_memory(settings: Settings) -> None:
    memory = AgentMemory(settings.history_turns)
    memory.record_turn(1, "hi", "hello")
    memory.touch_docs(1, [(412, "Car insurance 2026")])

    memory.clear(1)

    assert memory.history(1) == []
    assert memory.recent_docs(1) == []


async def test_recent_docs_cap_at_ten() -> None:
    memory = AgentMemory(history_turns=6)
    for doc_id in range(1, 13):
        memory.touch_docs(1, [(doc_id, f"Doc {doc_id}")])

    docs = memory.recent_docs(1)

    assert len(docs) == 10
    assert docs[0][0] == 3  # oldest two (1, 2) evicted
    assert docs[-1][0] == 12


# --- mentioned_docs extraction -------------------------------------------------


async def test_mentioned_docs_ignores_ids_not_touched_this_turn(
    settings: Settings, tmp_path: Path
) -> None:
    """The model could cite a stale/hallucinated id in its own text; only
    ids a tool call actually returned data for this turn are offered."""
    client = FakeAnthropicClient(
        [FakeMessage(content=[FakeTextBlock(text="See #999 for details")], stop_reason="end_turn")]
    )
    budget = make_budget_store(tmp_path)
    memory = AgentMemory(settings.history_turns)

    result = await run_agent(
        chat_id=1,
        user_text="hi",
        lang="en",
        anthropic_client=client,  # type: ignore[arg-type]
        settings=settings,
        paperless=FakePaperless(),  # type: ignore[arg-type]
        budget_store=budget,
        memory=memory,
        send_document_callback=noop_send_document,
    )

    assert result.mentioned_docs == []


async def test_mentioned_docs_preserves_first_mention_order_and_dedupes(
    settings: Settings, tmp_path: Path
) -> None:
    client = FakeAnthropicClient(
        [
            FakeMessage(
                content=[FakeToolUseBlock(id="t1", name="search_documents", input={"query": "x"})],
                stop_reason="tool_use",
            ),
            FakeMessage(
                content=[FakeTextBlock(text="#500 and #412 (also #500 again) match")],
                stop_reason="end_turn",
            ),
        ]
    )
    budget = make_budget_store(tmp_path)
    memory = AgentMemory(settings.history_turns)
    docs = [make_doc(id=412, title="Car insurance 2026"), make_doc(id=500, title="Invoice")]
    paperless = FakePaperless(search_results=(docs, 2))

    result = await run_agent(
        chat_id=1,
        user_text="find stuff",
        lang="en",
        anthropic_client=client,  # type: ignore[arg-type]
        settings=settings,
        paperless=paperless,  # type: ignore[arg-type]
        budget_store=budget,
        memory=memory,
        send_document_callback=noop_send_document,
    )

    assert result.mentioned_docs == [(500, "Invoice"), (412, "Car insurance 2026")]
