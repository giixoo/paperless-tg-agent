"""Allowlist enforcement for every incoming Telegram update.

Registered as a `TypeHandler(Update, ...)` in group -1 so it runs before
any command/message/callback handler, per SPEC §4.1 ("including callbacks").
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from typing import Any

from telegram import Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

logger = logging.getLogger(__name__)

AuthMiddleware = Callable[[Update, ContextTypes.DEFAULT_TYPE], Coroutine[Any, Any, None]]


def build_auth_middleware(allowed_users: list[int]) -> AuthMiddleware:
    allowed = set(allowed_users)

    async def _check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user is None or user.id not in allowed:
            logger.warning("Rejected update from disallowed user id=%s", user.id if user else None)
            raise ApplicationHandlerStop

    return _check
