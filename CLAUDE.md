# CLAUDE.md

Telegram bot for Paperless-ngx v3 with a Claude tool-use agent. Full requirements: **SPEC.md** — read it first, follow milestones in §10 in order.

## Clean-room rule (important)
This project is MIT-licensed and written from scratch.
- Do NOT copy, fetch, or adapt code from `GeiserX/paperless-telegram-bot` or any other GPL/AGPL project (e.g. `rabestro/paperless-genie`).
- Use only official docs: Paperless-ngx API docs/schema, python-telegram-bot docs, Anthropic SDK docs.

## Working agreement
- Ask before making decisions not covered by SPEC.md (new deps, changed behavior, schema guesses).
- When unsure about a Paperless API detail (API version, `custom_field_query` syntax, task response shape), say so and add a TODO + test, rather than guessing silently.
- Small, reviewable commits per milestone; conventional commit messages.
- Prefer editing existing files over regenerating whole files.

## Commands
```
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest -q
uv run python -m paperbot          # local run, needs .env
docker compose -f docker-compose.example.yml build
```

## Conventions
- Python 3.12, fully async, type hints everywhere, `mypy --strict` on `src/`.
- No heavy native deps: target CPU has no AVX (Celeron J4105-class). No torch/numpy-heavy libs.
- One shared `httpx.AsyncClient` for Paperless; one `AsyncAnthropic` client.
- Config only via `paperbot.config.Settings` (pydantic-settings); never read env elsewhere.
- Logging: no secrets, no document content, no user message text at INFO. Log doc ids, counts, durations, token usage.
- User-facing fixed strings live in `i18n.py` (uk, pl, ru, en); agent answers follow the user's language per SPEC §5.3.
- Tool implementations never raise into the agent loop; return `{"error": ...}`.
- Tags whose names start with `HIDDEN_TAG_PREFIXES` are never shown or offered to the model.
- Telegram `callback_data` ≤ 64 bytes.

## Testing
- Paperless: mock with `respx`; keep JSON fixtures in `tests/fixtures/` modeled on real v3 responses (ask the user to paste real samples if needed).
- Agent: inject a fake Anthropic client returning scripted `tool_use` / `end_turn` responses; assert tool calls, memory, budget accounting.
- No real network in tests.

## Done = 
ruff + mypy + pytest green, Docker image builds and runs as non-root, README documents setup and every env var, LICENSE is MIT.