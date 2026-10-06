from __future__ import annotations

from datetime import datetime

from paperbot.agent.prompts import build_system_prompt


def test_includes_formatted_date_and_weekday() -> None:
    now = datetime(2026, 10, 6)  # a Tuesday

    prompt = build_system_prompt(now, [])

    assert "06.10.2026" in prompt
    assert "Tuesday" in prompt


def test_includes_recent_docs_when_present() -> None:
    now = datetime(2026, 10, 6)

    prompt = build_system_prompt(now, [(412, "Car insurance 2026")])

    assert "#412 Car insurance 2026" in prompt


def test_omits_recent_docs_section_when_empty() -> None:
    now = datetime(2026, 10, 6)

    prompt = build_system_prompt(now, [])

    assert "Recently discussed" not in prompt
