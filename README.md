# paperless-tg-agent

A Telegram bot for a self-hosted [Paperless-ngx](https://docs.paperless-ngx.com/) archive:
full-text search, document cards, file upload/download, `/inbox` triage, expiry
reminders, near-duplicate review, and a Claude tool-use agent for
natural-language Q&A over your documents.

Full requirements: [Spec.md](Spec.md) (v0.1-v0.2) and [Spec-dups.md](Spec-dups.md)
(milestone 6, near-duplicate review).

**Status:** All milestones (1-6) implemented — config, Paperless client, access
control, commands (`/help` `/search` `/recent` `/doc` `/inbox` `/expiring` `/dups`
`/usage` `/clear`), file upload/download, the Claude agent, daily expiry
reminders, and near-duplicate document review (`/dups`, no LLM involved). See
**Known limitations** below before pointing this at a real server.

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
| `DUPS_ENABLED` | no | `true` | Enable the daily near-duplicate scan + `/dups` |
| `DUPS_SCAN_TIME` | no | `03:30` | Local time, daily |
| `DUPS_THRESHOLD` | no | `0.80` | Minimum text similarity for a candidate pair |
| `DUPS_NUMBERS_THRESHOLD` | no | `0.70` | Minimum number-token similarity (filters out same-template-different-year forms) |
| `DUPS_MIN_CHARS` | no | `200` | Documents with less extracted text are not compared |
| `DUPS_SKIP_TAGS` | no | `gpt-ocr,gpt-auto,sonnet-ocr,sonnet-auto` | Documents carrying one of these (still being OCR'd/classified) are not compared yet |
| `DUPS_NOTIFY` | no | `true` | Send a message when a scan finds new candidate pairs |
| `DUPS_NOTIFY_TIME` | no | `09:00` | Local time the automatic scan's notification is sent (deferred so it never lands overnight) |
| `TZ` | no | `Europe/Warsaw` | |
| `DATA_DIR` | no | `/data` | SQLite location |
| `HEALTH_PORT` | no | `8080` | `/health` HTTP endpoint |
| `LOG_LEVEL` | no | `INFO` | |

## Near-duplicate review (`/dups`)

Paperless only finds *exact* duplicates (same checksum). `/dups` finds
*near*-duplicates — e.g. the same document scanned twice at different
quality — with pure text/number similarity (Jaccard on word shingles +
number tokens); no LLM, no cost. A daily scan (`DUPS_SCAN_TIME`) and
`/dups scan` populate a queue of candidate pairs. New pairs found by the
*automatic* scan aren't announced right away (that could land in the middle
of the night) — they're held and sent in one message at `DUPS_NOTIFY_TIME`
instead; `/dups scan` run by hand still reports its result immediately.
Review pairs with:

- `/dups` — show the next open pair (highest similarity first), with
  download buttons, a ⭐ hint for the likely better copy, and `Keep A` /
  `Keep B` / `Not duplicates` / `Skip`.
- `Keep X` compares the kept copy against the other one and, if they differ
  in tags/correspondent/type/custom fields/notes, shows a checklist to pick
  what to copy over (`Copy all`, `Copy selected`, `Skip copy`, `Cancel`).
  Conflicting custom field values are flagged and only copied if explicitly
  checked.
- Once applied, the other copy is moved to the Paperless trash (kept there
  for 30 days) — never permanently deleted immediately. The confirmation
  message then shows a `Next dup` button if more pairs are open, or
  `All dups resolved` if that was the last one.
- `/dups stats` — open/resolved/dismissed counts and last scan time.
- `/dups undo` — restore the most recently trashed copy from the trash (the
  bot lists what was copied, for manual cleanup; it doesn't auto-reverse it).

## Known limitations

Most of the Paperless-ngx API shape has now been verified against a real
v3.2.x server through live use (tags incl. `is_inbox_tag`, custom field
`data_type` values and their monetary/date/string value shapes, the
document list/detail shape, the `custom_field_query` grammar — both
`exists` and `range` confirmed to correctly filter by field *name* — the
upload task response shape including `related_document` on SUCCESS, the
`post_document/` response body, and `tags__id__in` for `/inbox`) and is
pinned by `tests/fixtures/real_*.json` where practical. A couple of details
in that v0.1/v0.2 code remain unverified — marked with a `TODO(paperless-api)`
comment in the code, per CLAUDE.md's clean-room/no-guessing policy:

- The bulk-edit `modify_tags` parameter shape (the `/inbox` "✅ Done" button
  and tag toggles) — the request looks right but hasn't been confirmed to
  actually change anything on a real server yet.
- `update_document` (`PATCH /api/documents/{id}/`, used by the inbox
  correspondent/type/title editing) — standard DRF partial update, but not
  yet confirmed against a real server either.

For `/dups` (near-duplicate review, Spec-dups.md), a few more details are
marked `TODO(paperless-api)` in `paperbot/paperless.py`:

- `GET /api/documents/{id}/metadata/` field names (`original_size`,
  `archive_size`) — match the publicly documented serializer but aren't
  pinned against a real response for this endpoint yet.
- `POST /api/documents/{id}/notes/` body shape — guessed as `{"note": text}`
  to mirror how notes come back on the document detail endpoint.
- `DELETE /api/documents/{id}/` as a soft-delete (trash) and
  `POST /api/trash/` with `{"documents": [id], "action": "restore"}` for
  `/dups undo` — both match the documented Paperless-ngx 2.x+ trash API but
  aren't confirmed against this server.

The exact-duplicate side of the scan (`duplicate_documents` on the document
list/detail response) *is* confirmed — see `tests/fixtures/real_documents_list.json`
— so it's used directly instead of the `has_duplicates=true` filter the spec
originally suggested.

Everything else is tested against fixtures modeled on the public API docs or
pinned to real server responses (`tests/fixtures/`), with no real network
calls in the test suite.

## Out of scope (v1)

Per Spec.md §11: multi-user Paperless tokens (one shared token), editing
metadata from Telegram, combining album photos into one PDF, voice messages.

## License

MIT — see [LICENSE](LICENSE). Clean-room implementation; see
[CLAUDE.md](CLAUDE.md) for the policy on not reusing GPL/AGPL code.
