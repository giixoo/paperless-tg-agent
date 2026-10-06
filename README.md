# paperless-tg-agent

A Telegram bot for a self-hosted [Paperless-ngx](https://docs.paperless-ngx.com/) archive:
full-text search, document cards, file upload/download, `/inbox` triage, expiry
reminders, and a Claude tool-use agent for natural-language Q&A over your documents.

Full requirements: [Spec.md](Spec.md).

**Status:** All milestones (1-5) implemented per Spec.md §10 — config, Paperless
client, access control, commands (`/help` `/search` `/recent` `/doc` `/inbox`
`/expiring` `/usage` `/clear`), file upload/download, the Claude agent, and daily
expiry reminders. See **Known limitations** below before pointing this at a real
server.

## Setup

1. Copy `.env.example` to `.env` and fill in the required values (see table
   below).
2. Install dependencies and run locally:
   ```
   uv sync
   uv run python -m paperbot
   ```
3. Or, to deploy alongside an existing Paperless-ngx docker compose stack:
   merge the `telegram-bot` service from `docker-compose.example.yml` into
   your compose file (see that file's comments for details), then:
   ```
   docker compose build telegram-bot
   docker compose up -d telegram-bot
   ```
   For a standalone build/run instead:
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
| `ANTHROPIC_API_KEY` | yes | | Used by the agent |
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

## Known limitations

Most of the Paperless-ngx API shape has now been verified against a real
v3.2.x server (tags incl. `is_inbox_tag`, custom field `data_type` values and
their monetary/date/string value shapes, the document list/detail shape, and
the `custom_field_query` grammar — both `exists` and `range` confirmed to
correctly filter by field *name*, exactly as `find_by_custom_field`,
`/expiring`, and the reminder job use it) and is pinned by
`tests/fixtures/real_*.json`. A couple of details remain unverified — marked
with a `TODO(paperless-api)` comment in the code, per CLAUDE.md's clean-room/
no-guessing policy:

- **Task response shape** after an upload, including whether
  `related_document` is present on SUCCESS.
- **`post_document/` response body** (bare UUID string vs. `{"task_id": ...}`)
  — both are handled, neither confirmed.
- **`tags__id__in` filter** (`/inbox`) and the bulk-edit `modify_tags`
  parameter shape (the "✅ Done" button).

Everything else is tested against fixtures modeled on the public API docs or
pinned to real server responses (`tests/fixtures/`), with no real network
calls in the test suite.

## Out of scope (v1)

Per Spec.md §11: multi-user Paperless tokens (one shared token), editing
metadata from Telegram, combining album photos into one PDF, voice messages.

## License

MIT — see [LICENSE](LICENSE). Clean-room implementation; see
[CLAUDE.md](CLAUDE.md) for the policy on not reusing GPL/AGPL code.
