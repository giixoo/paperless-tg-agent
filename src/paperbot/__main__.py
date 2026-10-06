"""Entry point: build the Telegram application, start the health server,
run polling. `uv run python -m paperbot`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from pathlib import Path

import httpx
from anthropic import AsyncAnthropic
from telegram import Update
from telegram.ext import ApplicationBuilder, TypeHandler

from paperbot.agent.agent import AgentMemory
from paperbot.budget import BudgetStore, PriceConfig
from paperbot.config import Settings, load_settings
from paperbot.health import run_health_server
from paperbot.paperless import PaperlessClient
from paperbot.telegram.auth import build_auth_middleware
from paperbot.telegram.commands import register_handlers
from paperbot.telegram.deps import DEPS_KEY, Deps
from paperbot.telegram.files import register_file_handlers
from paperbot.telegram.keyboards import SearchStateStore

logger = logging.getLogger(__name__)


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


async def _run(settings: Settings) -> None:
    async with httpx.AsyncClient() as http_client:
        paperless = PaperlessClient(settings, http_client)

        paperless_ok = await paperless.ping()
        anthropic_key_present = bool(settings.anthropic_api_key)
        logger.info(
            "Startup checks: paperless_ok=%s anthropic_key_present=%s",
            paperless_ok,
            anthropic_key_present,
        )
        if not paperless_ok:
            logger.warning("Paperless is not reachable at startup; will retry on demand.")

        anthropic_client = AsyncAnthropic(api_key=settings.anthropic_api_key)
        budget_store = BudgetStore(
            Path(settings.data_dir) / "budget.sqlite3",
            PriceConfig(
                input_per_mtok=settings.price_input_per_mtok,
                output_per_mtok=settings.price_output_per_mtok,
                cache_write_per_mtok=settings.price_cache_write_per_mtok,
                cache_read_per_mtok=settings.price_cache_read_per_mtok,
            ),
        )
        agent_memory = AgentMemory(settings.history_turns)

        application = ApplicationBuilder().token(settings.telegram_bot_token).build()
        application.bot_data[DEPS_KEY] = Deps(
            settings=settings,
            paperless=paperless,
            search_store=SearchStateStore(),
            anthropic_client=anthropic_client,
            budget_store=budget_store,
            agent_memory=agent_memory,
        )
        application.add_handler(
            TypeHandler(Update, build_auth_middleware(settings.telegram_allowed_users)), group=-1
        )
        register_handlers(application)
        register_file_handlers(application)

        alive = True

        def is_alive() -> bool:
            return alive

        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig_name in ("SIGTERM", "SIGINT"):
            sig = getattr(signal, sig_name, None)
            if sig is None:
                continue
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop_event.set)

        try:
            async with application:
                await application.start()
                if application.updater is None:
                    raise RuntimeError("Application has no updater")
                await application.updater.start_polling()
                health_task = asyncio.create_task(run_health_server(is_alive, settings.health_port))
                logger.info("paperbot started")
                try:
                    await stop_event.wait()
                finally:
                    alive = False
                    health_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await health_task
                    await application.updater.stop()
                    await application.stop()
        finally:
            await anthropic_client.close()


def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    try:
        asyncio.run(_run(settings))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
