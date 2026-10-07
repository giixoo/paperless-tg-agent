"""Agent loop: memory, prompt caching, budget hooks (SPEC §5.1-5.2)."""

from __future__ import annotations

import asyncio
import logging
import re
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

import anthropic
from anthropic import AsyncAnthropic
from anthropic.types import MessageParam, TextBlockParam, ToolResultBlockParam

from paperbot.agent.prompts import build_system_prompt
from paperbot.agent.tools import (
    TOOL_SCHEMAS,
    SendDocumentCallback,
    dispatch_tool,
    extract_touched_docs,
)
from paperbot.budget import BudgetStore
from paperbot.config import Settings
from paperbot.i18n import t
from paperbot.paperless import PaperlessClient

logger = logging.getLogger(__name__)

RECENT_DOCS_MAX = 10
MENTIONED_DOCS_MAX = 5

_MENTION_RE = re.compile(r"#(\d+)")

Role = Literal["user", "assistant"]


@dataclass(slots=True)
class AgentResult:
    text: str
    mentioned_docs: list[tuple[int, str]] = field(default_factory=list)
    """Documents the bot's own reply text cites as "#id" AND that a tool
    call actually returned data for this turn (so we never offer a
    download/preview button for a hallucinated or stale id) - used to
    attach download/preview buttons to the reply."""


@dataclass(slots=True)
class ChatMemory:
    history: deque[tuple[Role, str]] = field(default_factory=deque)
    recent_docs: OrderedDict[int, str] = field(default_factory=OrderedDict)


class AgentMemory:
    """Per-chat, in-memory conversation history + recently touched docs.

    Lost on restart by design (SPEC §5.2).
    """

    def __init__(self, history_turns: int) -> None:
        self._history_turns = history_turns
        self._chats: dict[int, ChatMemory] = {}

    def _get(self, chat_id: int) -> ChatMemory:
        return self._chats.setdefault(chat_id, ChatMemory())

    def history(self, chat_id: int) -> list[tuple[Role, str]]:
        return list(self._get(chat_id).history)

    def recent_docs(self, chat_id: int) -> list[tuple[int, str]]:
        return list(self._get(chat_id).recent_docs.items())

    def record_turn(self, chat_id: int, user_text: str, assistant_text: str) -> None:
        mem = self._get(chat_id)
        mem.history.append(("user", user_text))
        mem.history.append(("assistant", assistant_text))
        while len(mem.history) > self._history_turns * 2:
            mem.history.popleft()

    def touch_docs(self, chat_id: int, docs: list[tuple[int, str]]) -> None:
        mem = self._get(chat_id)
        for doc_id, title in docs:
            mem.recent_docs.pop(doc_id, None)
            if title:
                mem.recent_docs[doc_id] = title
        while len(mem.recent_docs) > RECENT_DOCS_MAX:
            mem.recent_docs.popitem(last=False)

    def clear(self, chat_id: int) -> None:
        self._chats.pop(chat_id, None)


def _extract_text(content: list[Any]) -> str:
    parts = [block.text for block in content if getattr(block, "type", None) == "text"]
    return "\n".join(parts).strip()


def _extract_mentioned_docs(
    final_text: str, touched_this_turn: dict[int, str]
) -> list[tuple[int, str]]:
    """Doc ids the reply text cites as "#id", in first-mention order,
    restricted to ids a tool call actually returned this turn."""
    mentioned: list[tuple[int, str]] = []
    seen: set[int] = set()
    for match in _MENTION_RE.finditer(final_text):
        doc_id = int(match.group(1))
        if doc_id in seen or doc_id not in touched_this_turn:
            continue
        seen.add(doc_id)
        mentioned.append((doc_id, touched_this_turn[doc_id]))
        if len(mentioned) >= MENTIONED_DOCS_MAX:
            break
    return mentioned


async def _record_usage(budget_store: BudgetStore, settings: Settings, usage: Any) -> None:
    await budget_store.record_usage(
        tz_name=settings.tz,
        input_tokens=usage.input_tokens or 0,
        output_tokens=usage.output_tokens or 0,
        cache_write_tokens=usage.cache_creation_input_tokens or 0,
        cache_read_tokens=usage.cache_read_input_tokens or 0,
    )


async def run_agent(
    *,
    chat_id: int,
    user_text: str,
    lang: str,
    anthropic_client: AsyncAnthropic,
    settings: Settings,
    paperless: PaperlessClient,
    budget_store: BudgetStore,
    memory: AgentMemory,
    send_document_callback: SendDocumentCallback,
) -> AgentResult:
    today = await budget_store.today_usage(settings.tz)
    if today.cost_usd >= settings.daily_budget_usd:
        return AgentResult(text=t("budget_reached", lang))

    now = datetime.now(ZoneInfo(settings.tz))
    system_prompt = build_system_prompt(now, memory.recent_docs(chat_id))
    system_blocks: list[TextBlockParam] = [
        {"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}
    ]

    messages: list[MessageParam] = [
        {"role": role, "content": text} for role, text in memory.history(chat_id)
    ]
    messages.append({"role": "user", "content": user_text})

    final_text: str | None = None
    touched_this_turn: dict[int, str] = {}
    for _step in range(settings.agent_max_steps):
        try:
            response = await anthropic_client.messages.create(
                model=settings.llm_model,
                max_tokens=settings.agent_max_tokens,
                system=system_blocks,
                tools=TOOL_SCHEMAS,
                messages=messages,
            )
        except anthropic.APIError as exc:
            logger.warning("Anthropic API error: %s", type(exc).__name__)
            return AgentResult(text=t("generic_error", lang))

        await _record_usage(budget_store, settings, response.usage)
        messages.append({"role": "assistant", "content": response.content})

        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
        if response.stop_reason != "tool_use" or not tool_use_blocks:
            final_text = _extract_text(response.content)
            break

        results = await asyncio.gather(
            *(
                dispatch_tool(
                    block.name,
                    block.input if isinstance(block.input, dict) else {},
                    paperless=paperless,
                    settings=settings,
                    send_document_callback=send_document_callback,
                )
                for block in tool_use_blocks
            )
        )

        touched: list[tuple[int, str]] = []
        tool_result_blocks: list[ToolResultBlockParam] = []
        for block, result_json in zip(tool_use_blocks, results, strict=True):
            tool_result_blocks.append(
                {"type": "tool_result", "tool_use_id": block.id, "content": result_json}
            )
            touched.extend(extract_touched_docs(block.name, result_json))
        if touched:
            memory.touch_docs(chat_id, touched)
            touched_this_turn.update(touched)
        messages.append({"role": "user", "content": tool_result_blocks})
    else:
        # AGENT_MAX_STEPS tool_use iterations exhausted: ask once more, no tools.
        try:
            response = await anthropic_client.messages.create(
                model=settings.llm_model,
                max_tokens=settings.agent_max_tokens,
                system=system_blocks,
                messages=messages,
            )
        except anthropic.APIError as exc:
            logger.warning("Anthropic API error: %s", type(exc).__name__)
            return AgentResult(text=t("generic_error", lang))
        await _record_usage(budget_store, settings, response.usage)
        final_text = _extract_text(response.content)

    final_text = final_text or ""
    memory.record_turn(chat_id, user_text, final_text)
    mentioned_docs = _extract_mentioned_docs(final_text, touched_this_turn)
    return AgentResult(text=final_text, mentioned_docs=mentioned_docs)
