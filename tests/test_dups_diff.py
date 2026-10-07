from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from paperbot.dups.diff import (
    KIND_CONFLICT,
    KIND_CORRESPONDENT,
    KIND_CUSTOM_FIELD,
    KIND_DOCUMENT_TYPE,
    KIND_NOTE,
    KIND_TAG,
    DiffItem,
    apply_selected_items,
    build_diff,
    non_conflict_indices,
)
from paperbot.paperless import Correspondent, CustomFieldDef, Document, DocumentType, Tag


class FakeTaxonomy:
    def __init__(
        self,
        tags: list[Tag] | None = None,
        correspondents: list[Correspondent] | None = None,
        document_types: list[DocumentType] | None = None,
        custom_fields: list[CustomFieldDef] | None = None,
    ) -> None:
        self._tags = tags or []
        self._correspondents = correspondents or []
        self._document_types = document_types or []
        self._custom_fields = custom_fields or []

    async def resolve_tag_id(self, name: str) -> int | None:
        for tag in self._tags:
            if tag.name == name:
                return tag.id
        return None

    async def resolve_correspondent_id(self, name: str) -> int | None:
        for c in self._correspondents:
            if c.name == name:
                return c.id
        return None

    async def resolve_document_type_id(self, name: str) -> int | None:
        for d in self._document_types:
            if d.name == name:
                return d.id
        return None

    async def all_custom_fields(self) -> list[CustomFieldDef]:
        return self._custom_fields


def make_doc(**overrides: Any) -> Document:
    defaults: dict[str, Any] = dict(
        id=1,
        title="Doc",
        created=date(2026, 1, 1),
        correspondent=None,
        document_type=None,
        tags=[],
        custom_fields={},
        custom_fields_raw=[],
        notes_list=[],
    )
    defaults.update(overrides)
    return Document(**defaults)


@dataclass
class FakePaperless:
    bulk_modify_calls: list[tuple[list[int], list[int], list[int]]] = field(default_factory=list)
    update_calls: list[tuple[int, dict[str, Any]]] = field(default_factory=list)
    note_calls: list[tuple[int, str]] = field(default_factory=list)

    async def bulk_modify_tags(
        self,
        document_ids: list[int],
        *,
        add_tags: list[int] | None = None,
        remove_tags: list[int] | None = None,
    ) -> None:
        self.bulk_modify_calls.append((document_ids, add_tags or [], remove_tags or []))

    async def update_document(self, doc_id: int, **fields: Any) -> None:
        self.update_calls.append((doc_id, fields))

    async def add_note(self, doc_id: int, text: str) -> None:
        self.note_calls.append((doc_id, text))


CUSTOM_FIELDS = [
    CustomFieldDef(id=30, name="Expires", data_type="date"),
    CustomFieldDef(id=31, name="Policy / Doc number", data_type="string"),
    CustomFieldDef(id=32, name="Amount", data_type="monetary"),
]


# --- build_diff ----------------------------------------------------------------


async def test_build_diff_empty_for_identical_docs() -> None:
    taxonomy = FakeTaxonomy()
    survivor = make_doc(id=1)
    loser = make_doc(id=2)

    items = await build_diff(survivor, loser, taxonomy)  # type: ignore[arg-type]

    assert items == []


async def test_build_diff_adds_tag_present_only_on_loser() -> None:
    tags = [Tag(id=5, name="Car")]
    taxonomy = FakeTaxonomy(tags=tags)
    survivor = make_doc(id=1, tags=[])
    loser = make_doc(id=2, tags=["Car"])

    items = await build_diff(survivor, loser, taxonomy)  # type: ignore[arg-type]

    assert len(items) == 1
    assert items[0].kind == KIND_TAG
    assert items[0].tag_id == 5


async def test_build_diff_skips_tag_already_present_on_survivor() -> None:
    tags = [Tag(id=5, name="Car")]
    taxonomy = FakeTaxonomy(tags=tags)
    survivor = make_doc(id=1, tags=["Car"])
    loser = make_doc(id=2, tags=["Car"])

    items = await build_diff(survivor, loser, taxonomy)  # type: ignore[arg-type]

    assert items == []


async def test_build_diff_skips_tag_unresolvable_in_taxonomy() -> None:
    """A tag name that can't be resolved (e.g. it's hidden, or was deleted
    since the scan) must never produce a diff item."""
    taxonomy = FakeTaxonomy(tags=[])
    survivor = make_doc(id=1, tags=[])
    loser = make_doc(id=2, tags=["gpt-done"])

    items = await build_diff(survivor, loser, taxonomy)  # type: ignore[arg-type]

    assert items == []


async def test_build_diff_sets_correspondent_when_survivor_has_none() -> None:
    correspondents = [Correspondent(id=20, name="PZU")]
    taxonomy = FakeTaxonomy(correspondents=correspondents)
    survivor = make_doc(id=1, correspondent=None)
    loser = make_doc(id=2, correspondent="PZU")

    items = await build_diff(survivor, loser, taxonomy)  # type: ignore[arg-type]

    assert len(items) == 1
    assert items[0].kind == KIND_CORRESPONDENT
    assert items[0].correspondent_id == 20


async def test_build_diff_never_overwrites_survivor_correspondent() -> None:
    correspondents = [Correspondent(id=20, name="PZU"), Correspondent(id=21, name="Allianz")]
    taxonomy = FakeTaxonomy(correspondents=correspondents)
    survivor = make_doc(id=1, correspondent="Allianz")
    loser = make_doc(id=2, correspondent="PZU")

    items = await build_diff(survivor, loser, taxonomy)  # type: ignore[arg-type]

    assert items == []


async def test_build_diff_sets_document_type_when_survivor_has_none() -> None:
    doc_types = [DocumentType(id=10, name="Insurance policy")]
    taxonomy = FakeTaxonomy(document_types=doc_types)
    survivor = make_doc(id=1, document_type=None)
    loser = make_doc(id=2, document_type="Insurance policy")

    items = await build_diff(survivor, loser, taxonomy)  # type: ignore[arg-type]

    assert len(items) == 1
    assert items[0].kind == KIND_DOCUMENT_TYPE
    assert items[0].document_type_id == 10


async def test_build_diff_adds_custom_field_missing_on_survivor() -> None:
    taxonomy = FakeTaxonomy(custom_fields=CUSTOM_FIELDS)
    survivor = make_doc(id=1, custom_fields_raw=[])
    loser = make_doc(id=2, custom_fields_raw=[{"field": 30, "value": "2026-12-14"}])

    items = await build_diff(survivor, loser, taxonomy)  # type: ignore[arg-type]

    assert len(items) == 1
    assert items[0].kind == KIND_CUSTOM_FIELD
    assert items[0].custom_field_id == 30
    assert items[0].custom_field_value == "2026-12-14"
    assert "Expires" in items[0].label
    assert "14.12.2026" in items[0].label  # formatted per data_type


async def test_build_diff_flags_conflicting_custom_field_values() -> None:
    taxonomy = FakeTaxonomy(custom_fields=CUSTOM_FIELDS)
    survivor = make_doc(id=1, custom_fields_raw=[{"field": 30, "value": "2026-12-14"}])
    loser = make_doc(id=2, custom_fields_raw=[{"field": 30, "value": "2027-12-14"}])

    items = await build_diff(survivor, loser, taxonomy)  # type: ignore[arg-type]

    assert len(items) == 1
    assert items[0].kind == KIND_CONFLICT
    assert items[0].is_conflict is True
    assert items[0].custom_field_value == "2027-12-14"
    assert "S=14.12.2026" in items[0].label
    assert "L=14.12.2027" in items[0].label


async def test_build_diff_skips_custom_field_with_identical_value() -> None:
    taxonomy = FakeTaxonomy(custom_fields=CUSTOM_FIELDS)
    survivor = make_doc(id=1, custom_fields_raw=[{"field": 30, "value": "2026-12-14"}])
    loser = make_doc(id=2, custom_fields_raw=[{"field": 30, "value": "2026-12-14"}])

    items = await build_diff(survivor, loser, taxonomy)  # type: ignore[arg-type]

    assert items == []


async def test_build_diff_adds_one_note_item_for_all_loser_notes() -> None:
    taxonomy = FakeTaxonomy()
    survivor = make_doc(id=1, notes_list=[])
    loser = make_doc(id=2, notes_list=["First note", "Second note"])

    items = await build_diff(survivor, loser, taxonomy)  # type: ignore[arg-type]

    assert len(items) == 1
    assert items[0].kind == KIND_NOTE
    assert items[0].note_texts == ["First note", "Second note"]


async def test_build_diff_combines_all_item_kinds() -> None:
    taxonomy = FakeTaxonomy(
        tags=[Tag(id=5, name="Car")],
        correspondents=[Correspondent(id=20, name="PZU")],
        document_types=[DocumentType(id=10, name="Insurance policy")],
        custom_fields=CUSTOM_FIELDS,
    )
    survivor = make_doc(
        id=1,
        tags=[],
        correspondent=None,
        document_type=None,
        custom_fields_raw=[{"field": 30, "value": "2026-12-14"}],
        notes_list=[],
    )
    loser = make_doc(
        id=2,
        tags=["Car"],
        correspondent="PZU",
        document_type="Insurance policy",
        custom_fields_raw=[
            {"field": 30, "value": "2027-12-14"},
            {"field": 31, "value": "2840-8002"},
        ],
        notes_list=["Renewed early"],
    )

    items = await build_diff(survivor, loser, taxonomy)  # type: ignore[arg-type]

    kinds = [item.kind for item in items]
    assert kinds.count(KIND_TAG) == 1
    assert kinds.count(KIND_CORRESPONDENT) == 1
    assert kinds.count(KIND_DOCUMENT_TYPE) == 1
    assert kinds.count(KIND_CUSTOM_FIELD) == 1
    assert kinds.count(KIND_NOTE) == 1
    assert kinds.count(KIND_CONFLICT) == 1


# --- DiffItem (de)serialization -------------------------------------------------


def test_diff_item_roundtrips_through_dict() -> None:
    item = DiffItem(
        kind=KIND_CUSTOM_FIELD,
        label="Expires: 14.12.2026",
        custom_field_id=30,
        custom_field_value="x",
    )

    restored = DiffItem.from_dict(item.to_dict())

    assert restored == item


# --- non_conflict_indices -------------------------------------------------------


def test_non_conflict_indices_excludes_conflicts() -> None:
    items = [
        DiffItem(kind=KIND_TAG, label="tag: a"),
        DiffItem(kind=KIND_CONFLICT, label="conflict", is_conflict=True),
        DiffItem(kind=KIND_NOTE, label="note"),
    ]

    assert non_conflict_indices(items) == {0, 2}


# --- apply_selected_items --------------------------------------------------------


async def test_apply_selected_items_adds_tag() -> None:
    paperless = FakePaperless()
    survivor = make_doc(id=1)
    items = [DiffItem(kind=KIND_TAG, label="tag: Car", tag_id=5)]

    applied = await apply_selected_items(paperless, survivor, 2, items, {0})  # type: ignore[arg-type]

    assert applied == ["tag: Car"]
    assert paperless.bulk_modify_calls == [([1], [5], [])]


async def test_apply_selected_items_sets_correspondent() -> None:
    paperless = FakePaperless()
    survivor = make_doc(id=1)
    items = [DiffItem(kind=KIND_CORRESPONDENT, label="correspondent: PZU", correspondent_id=20)]

    await apply_selected_items(paperless, survivor, 2, items, {0})  # type: ignore[arg-type]

    assert paperless.update_calls == [(1, {"correspondent": 20})]


async def test_apply_selected_items_adds_custom_field_preserving_existing() -> None:
    paperless = FakePaperless()
    survivor = make_doc(id=1, custom_fields_raw=[{"field": 31, "value": "existing"}])
    items = [
        DiffItem(kind=KIND_CUSTOM_FIELD, label="x", custom_field_id=30, custom_field_value="v")
    ]

    await apply_selected_items(paperless, survivor, 2, items, {0})  # type: ignore[arg-type]

    assert len(paperless.update_calls) == 1
    doc_id, fields = paperless.update_calls[0]
    assert doc_id == 1
    assert {"field": 31, "value": "existing"} in fields["custom_fields"]
    assert {"field": 30, "value": "v"} in fields["custom_fields"]


async def test_apply_selected_items_conflict_replaces_survivor_value() -> None:
    paperless = FakePaperless()
    survivor = make_doc(id=1, custom_fields_raw=[{"field": 30, "value": "old"}])
    items = [
        DiffItem(
            kind=KIND_CONFLICT,
            label="x",
            custom_field_id=30,
            custom_field_value="new",
            is_conflict=True,
        )
    ]

    await apply_selected_items(paperless, survivor, 2, items, {0})  # type: ignore[arg-type]

    _, fields = paperless.update_calls[0]
    assert fields["custom_fields"] == [{"field": 30, "value": "new"}]


async def test_apply_selected_items_adds_notes_with_prefix() -> None:
    paperless = FakePaperless()
    survivor = make_doc(id=1)
    items = [DiffItem(kind=KIND_NOTE, label="note", note_texts=["First", "Second"])]

    await apply_selected_items(paperless, survivor, 99, items, {0})  # type: ignore[arg-type]

    assert paperless.note_calls == [(1, "From #99: First"), (1, "From #99: Second")]


async def test_apply_selected_items_skips_unselected_items() -> None:
    paperless = FakePaperless()
    survivor = make_doc(id=1)
    items = [DiffItem(kind=KIND_TAG, label="tag: Car", tag_id=5)]

    applied = await apply_selected_items(paperless, survivor, 2, items, set())  # type: ignore[arg-type]

    assert applied == []
    assert paperless.bulk_modify_calls == []
    assert paperless.update_calls == []
