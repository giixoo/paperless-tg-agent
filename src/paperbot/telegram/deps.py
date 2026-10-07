"""Shared, app-wide dependencies stashed in `Application.bot_data`.

Split out from commands.py so commands/files/agent handler modules can all
depend on it without creating import cycles between each other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

from anthropic import AsyncAnthropic
from telegram.ext import ContextTypes

from paperbot.agent.agent import AgentMemory
from paperbot.budget import BudgetStore
from paperbot.config import Settings
from paperbot.dups.db import DupsStore
from paperbot.paperless import PaperlessClient
from paperbot.telegram.keyboards import SearchStateStore
from paperbot.telegram.state import DupsSkipStore, PendingInputStore

DEPS_KEY = "deps"


@dataclass(slots=True)
class Deps:
    settings: Settings
    paperless: PaperlessClient
    search_store: SearchStateStore
    anthropic_client: AsyncAnthropic
    budget_store: BudgetStore
    agent_memory: AgentMemory
    pending_input: PendingInputStore
    dups_store: DupsStore | None = None
    dups_skip_store: DupsSkipStore = field(default_factory=DupsSkipStore)


def get_deps(context: ContextTypes.DEFAULT_TYPE) -> Deps:
    return cast(Deps, context.application.bot_data[DEPS_KEY])
