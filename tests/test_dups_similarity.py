from __future__ import annotations

from paperbot.dups.similarity import (
    jaccard,
    normalize_text,
    number_tokens,
    shingles,
    text_length_ratio_ok,
)

UK_ORIGINAL = (
    "Договір страхування автомобіля між страхувальником та страховою компанією ПЗУ. "
    "Поліс діє від дати підписання до чотирнадцятого грудня дві тисячі двадцять шостого року. "
    "Номер поліса два вісім чотири нуль тире вісім нуль нуль два. "
    "Сума страхового відшкодування визначається згідно з умовами договору та додатками до нього. "
    "Дата видачі документа: 14.12.2026. Номер поліса: 2840-8002. "
    "Будь ласка, зберігайте цей документ протягом усього строку дії договору страхування."
)
# A near-duplicate scan: a few isolated OCR-style character slips/typos and
# dropped trailing punctuation, not a systematic rewrite - shingling is
# designed to tolerate exactly this kind of sparse noise.
UK_NOISY_COPY = (
    "Договір страхування автомобіля мiж страхувальником та страховою компанією ПЗУ. "
    "Поліс діє від дати підписання до чотирнадцятого грудня дві тисячі двадцять шостого року. "
    "Номер поліса два вісім чотири нуль тире вісім нуль нуль два "
    "Сума страхового відшкодування визначається згідно з умовами договору та додатками до ньoго. "
    "Дата видачі документа: 14.12.2026. Номер поліса: 2840-8002. "
    "Будь ласка, зберігайте цей документ протягом усього строку дії договору страхування"
)

PL_ORIGINAL = (
    "Umowa ubezpieczenia samochodu zawarta pomiedzy ubezpieczajacym a towarzystwem PZU. "
    "Polisa obowiazuje od daty podpisania do czternastego grudnia dwa tysiace dwadziescia "
    "szostego roku. Numer polisy dwa osiem cztery zero kreska osiem zero zero dwa. "
    "Wysokosc odszkodowania okreslaja warunki umowy oraz zalaczniki do niej dolaczone. "
    "Data wystawienia dokumentu: 14.12.2026. Numer polisy: 2840-8002. "
    "Prosimy zachowac ten dokument przez caly okres obowiazywania umowy ubezpieczenia."
)
PL_NOISY_COPY = (
    "Umowa ubezpieczenia samochodu zawarta pomiedzy ubezpieczajacym a towarzystwem PZU "
    "Polisa obowiazuje od daty podpisania do czternastego grudnia dwa tysiace dwadziescia "
    "szostego roku. Numer polisy dwa osiem cztery zero kreska osiem zero zero dwa. "
    "Wysokosc odszkodowania okreslaja warunki umowy oraz zalaczniki do niej dolaczone "
    "Data wystawienia dokumentu: 14.12.2026. Numer polisy: 2840-8002. "
    "Prosimy zachowac ten dokument przez caly okres obowiazywania umowy ubezpieczenia"
)


def _sim(a: str, b: str) -> tuple[float, float]:
    text_sim = jaccard(shingles(normalize_text(a)), shingles(normalize_text(b)))
    num_sim = jaccard(number_tokens(a), number_tokens(b))
    return text_sim, num_sim


def test_noisy_uk_copy_scores_above_threshold() -> None:
    text_sim, num_sim = _sim(UK_ORIGINAL, UK_NOISY_COPY)
    assert text_sim >= 0.80
    assert num_sim >= 0.70


def test_noisy_pl_copy_scores_above_threshold() -> None:
    text_sim, num_sim = _sim(PL_ORIGINAL, PL_NOISY_COPY)
    assert text_sim >= 0.80
    assert num_sim >= 0.70


_FORM_BOILERPLATE = (
    "This is an official tax declaration form issued by the local tax office for "
    "the fiscal year under review. The taxpayer identified in the records below "
    "must settle the stated amount by the specified deadline printed on this "
    "notice. Failure to pay by the deadline may result in additional penalties "
    "and interest charges being applied to the outstanding balance. Please keep "
    "this document for your personal records and contact the regional tax "
    "office if you have any questions regarding this notice. This notice was "
    "generated automatically and does not require a signature to be considered "
    "valid for the purposes described above. "
)


def test_same_template_different_numbers_fails_number_gate() -> None:
    """Two tax forms using the same template text but different years/amounts
    must not become candidates (SPEC-dups §3.2.3)."""
    form_2025 = _FORM_BOILERPLATE + "Year: 2025. Amount due: 1200.50 USD. Reference: AB-2025-001."
    form_2026 = _FORM_BOILERPLATE + "Year: 2026. Amount due: 1450.75 USD. Reference: AB-2026-002."

    text_sim, num_sim = _sim(form_2025, form_2026)

    assert text_sim >= 0.80  # same template -> high text similarity
    assert num_sim < 0.70  # but numbers differ -> must not pass the gate


def test_normalize_text_strips_punctuation_and_collapses_spaces() -> None:
    assert normalize_text("Hello,   World!!") == "hello world"


def test_normalize_text_applies_nfkc_and_lowercase() -> None:
    assert normalize_text("CAFÉ") == "café"


def test_shingles_of_short_text_returns_single_shingle() -> None:
    assert shingles(normalize_text("one two")) == {"one two"}


def test_shingles_empty_text_returns_empty_set() -> None:
    assert shingles("") == set()


def test_number_tokens_preserves_date_separators() -> None:
    tokens = number_tokens("Valid until 14.12.2026 amount $12.30")
    assert "14.12.2026" in tokens
    assert "$12.30" in tokens


def test_jaccard_both_empty_is_one() -> None:
    assert jaccard(set(), set()) == 1.0


def test_jaccard_disjoint_is_zero() -> None:
    assert jaccard({"a"}, {"b"}) == 0.0


def test_jaccard_identical_is_one() -> None:
    assert jaccard({"a", "b"}, {"a", "b"}) == 1.0


def test_text_length_ratio_ok_rejects_below_half() -> None:
    assert text_length_ratio_ok(100, 40) is False
    assert text_length_ratio_ok(100, 60) is True


def test_text_length_ratio_ok_handles_zero_length() -> None:
    assert text_length_ratio_ok(0, 0) is True
    assert text_length_ratio_ok(0, 10) is False
