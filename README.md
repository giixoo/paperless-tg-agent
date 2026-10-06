# paperless-tg-agent

A Telegram bot for a self-hosted [Paperless-ngx](https://docs.paperless-ngx.com/) archive:
full-text search, document cards, and (coming in a later milestone) a Claude
tool-use agent for natural-language Q&A over your documents.

Full requirements: [Spec.md](Spec.md).

**Status:** Milestone 1 (skeleton) — config, Paperless client, access control,
`/help`, `/search`, `/recent`, `/doc`, health endpoint, Docker image. Upload/
download, the agent, `/inbox`, `/expiring` and reminders land in later
milestones (see Spec.md §10).

## Setup

1. Copy `.env.example` to `.env` and fill in the required values (see table
   below).
2. Install dependencies and run locally:
   ```
   uv sync
   uv run python -m paperbot
   ```
3. Or build and run with Docker, alongside your existing Paperless-ngx
   compose stack (see `docker-compose.example.yml` once added in a later
   milestone):
   ```
   docker build -t paperless-tg-agent .
   docker run --rm --env-file .env -v $(pwd)/bot-data:/data paperless-tg-agent
   ```

## Development

```
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest -q
```

## Configuration

All configuration is via environment variables (see `.env.example`).

| Var | Required | Default | Notes |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | | Telegram bot token from @BotFather |
| `TELEGRAM_ALLOWED_USERS` | yes | | Comma-separated Telegram user IDs; everyone else is silently ignored |
| `PAPERLESS_URL` | yes | | e.g. `http://webserver:8000` |
| `PAPERLESS_PUBLIC_URL` | no | `PAPERLESS_URL` | Used for clickable links in replies |
| `PAPERLESS_TOKEN` | yes | | Paperless API token |
| `PAPERLESS_API_VERSION` | no | `9` | Sent as `Accept: application/json; version=N` |
| `ANTHROPIC_API_KEY` | yes | | Used by the agent (later milestone) |
| `LLM_MODEL` | no | `claude-haiku-4-5` | |
| `AGENT_MAX_STEPS` | no | `5` | Max tool-use iterations per question |
| `AGENT_MAX_TOKENS` | no | `1024` | Output cap per LLM call |
| `DOC_CONTENT_MAX_CHARS` | no | `3000` | Truncation for document content shown to the agent |
| `HISTORY_TURNS` | no | `6` | Per-chat agent memory (turns) |
| `DAILY_BUDGET_USD` | no | `0.20` | Hard cap on LLM spend per local day |
| `PRICE_INPUT_PER_MTOK` | no | `1.00` | USD per million input tokens |
| `PRICE_OUTPUT_PER_MTOK` | no | `5.00` | USD per million output tokens |
| `PRICE_CACHE_WRITE_PER_MTOK` | no | `1.25` | USD per million cache-write tokens |
| `PRICE_CACHE_READ_PER_MTOK` | no | `0.10` | USD per million cache-read tokens |
| `EXPIRY_FIELD_NAME` | no | `Expires` | Custom field used by `/expiring` + reminders |
| `REMINDER_ENABLED` | no | `true` | |
| `REMINDER_TIME` | no | `09:00` | Local time, daily |
| `REMINDER_DAYS` | no | `30` | Warn about documents expiring within N days |
| `HIDDEN_TAG_PREFIXES` | no | `gpt,sonnet` | Tags hidden from output and the agent |
| `TZ` | no | `Europe/Warsaw` | |
| `DATA_DIR` | no | `/data` | SQLite location |
| `HEALTH_PORT` | no | `8080` | `/health` HTTP endpoint |
| `LOG_LEVEL` | no | `INFO` | |

## License

MIT — see [LICENSE](LICENSE). Clean-room implementation; see
[CLAUDE.md](CLAUDE.md) for the policy on not reusing GPL/AGPL code.
