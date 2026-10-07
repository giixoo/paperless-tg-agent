"""Metadata diff builder and applier for the near-duplicate review flow
(SPEC-dups §5.2, §5.4). Hidden tags (`HIDDEN_TAG_PREFIXES`) never appear here
because `Document.tags`/`.correspondent`/`.document_type` are already
filtered by `TaxonomyCache`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from paperbot.paperless import Document, PaperlessClient, TaxonomyCache, _format_custom_field_value

KIND_TAG = "tag"
KIND_CORRESPONDENT = "correspondent"
KIND_DOCUMENT_TYPE = "document_type"
KIND_CUSTOM_FIELD = "custom_field"
KIND_NOTE = "note"
KIND_CONFLICT = "conflict"


@dataclass(slots=True)
class DiffItem:
    kind: str
    label: str
    tag_id: int | None = None
    correspondent_id: int | None = None
    document_type_id: int | None = None
    custom_field_id: int | None = None
    custom_field_value: Any = None
    note_texts: list[str] = field(default_factory=list)
    is_conflict: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "tag_id": self.tag_id,
            "correspondent_id": self.correspondent_id,
            "document_type_id": self.document_type_id,
            "custom_field_id": self.custom_field_id,
            "custom_field_value": self.custom_field_value,
            "note_texts": self.note_texts,
            "is_conflict": self.is_conflict,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DiffItem:
        return cls(
            kind=data["kind"],
            label=data["label"],
            tag_id=data.get("tag_id"),
            correspondent_id=data.get("correspondent_id"),
            document_type_id=data.get("document_type_id"),
            custom_field_id=data.get("custom_field_id"),
            custom_field_value=data.get("custom_field_value"),
            note_texts=list(data.get("note_texts") or []),
            is_conflict=bool(data.get("is_conflict", False)),
        )


async def build_diff(
    survivor: Document, loser: Document, taxonomy: TaxonomyCache
) -> list[DiffItem]:
    """Build the list of copyable items from `loser` (L) to `survivor` (S).
    Order matches the SPEC-dups §5.2 table: tag, correspondent, document
    type, custom field, note, conflict."""
    items: list[DiffItem] = []

    survivor_tag_names = set(survivor.tags)
    for tag_name in loser.tags:
        if tag_name in survivor_tag_names:
            continue
        tag_id = await taxonomy.resolve_tag_id(tag_name)
        if tag_id is not None:
            items.append(DiffItem(kind=KIND_TAG, label=f"tag: {tag_name}", tag_id=tag_id))

    if not survivor.correspondent and loser.correspondent:
        correspondent_id = await taxonomy.resolve_correspondent_id(loser.correspondent)
        if correspondent_id is not None:
            items.append(
                DiffItem(
                    kind=KIND_CORRESPONDENT,
                    label=f"correspondent: {loser.correspondent}",
                    correspondent_id=correspondent_id,
                )
            )

    if not survivor.document_type and loser.document_type:
        type_id = await taxonomy.resolve_document_type_id(loser.document_type)
        if type_id is not None:
            items.append(
                DiffItem(
                    kind=KIND_DOCUMENT_TYPE,
                    label=f"document type: {loser.document_type}",
                    document_type_id=type_id,
                )
            )

    custom_field_defs = {f.id: f for f in await taxonomy.all_custom_fields()}
    survivor_fields = {
        e["field"]: e.get("value") for e in survivor.custom_fields_raw if "field" in e
    }
    loser_fields = {e["field"]: e.get("value") for e in loser.custom_fields_raw if "field" in e}

    custom_field_items: list[DiffItem] = []
    conflict_items: list[DiffItem] = []
    for field_id, loser_value in loser_fields.items():
        field_def = custom_field_defs.get(field_id)
        name = field_def.name if field_def else f"field #{field_id}"
        data_type = field_def.data_type if field_def else "string"
        if field_id not in survivor_fields:
            display = _format_custom_field_value(loser_value, data_type)
            custom_field_items.append(
                DiffItem(
                    kind=KIND_CUSTOM_FIELD,
                    label=f"{name}: {display}",
                    custom_field_id=field_id,
                    custom_field_value=loser_value,
                )
            )
        elif survivor_fields[field_id] != loser_value:
            s_display = _format_custom_field_value(survivor_fields[field_id], data_type)
            l_display = _format_custom_field_value(loser_value, data_type)
            conflict_items.append(
                DiffItem(
                    kind=KIND_CONFLICT,
                    label=f"⚠ {name}: S={s_display}, L={l_display}",
                    custom_field_id=field_id,
                    custom_field_value=loser_value,
                    is_conflict=True,
                )
            )
    items.extend(custom_field_items)

    if loser.notes_list:
        items.append(
            DiffItem(
                kind=KIND_NOTE,
                label=f"note: {len(loser.notes_list)} from #{loser.id}",
                note_texts=list(loser.notes_list),
            )
        )

    items.extend(conflict_items)
    return items


async def apply_selected_items(
    paperless: PaperlessClient,
    survivor: Document,
    loser_id: int,
    items: list[DiffItem],
    selected: set[int],
) -> list[str]:
    """Apply the selected diff items to `survivor` (SPEC-dups §5.4.1).
    Returns the applied item labels. Never overwrites a survivor value
    except for a selected conflict item (§5.2 rules). Raises
    `PaperlessError` on failure — callers must not trash the loser if this
    raises (§5.4.2).
    """
    applied: list[str] = []
    tag_ids: list[int] = []
    update_fields: dict[str, Any] = {}
    custom_fields_patch: list[dict[str, Any]] | None = None

    for idx, item in enumerate(items):
        if idx not in selected:
            continue
        if item.kind == KIND_TAG and item.tag_id is not None:
            tag_ids.append(item.tag_id)
            applied.append(item.label)
        elif item.kind == KIND_CORRESPONDENT and item.correspondent_id is not None:
            update_fields["correspondent"] = item.correspondent_id
            applied.append(item.label)
        elif item.kind == KIND_DOCUMENT_TYPE and item.document_type_id is not None:
            update_fields["document_type"] = item.document_type_id
            applied.append(item.label)
        elif item.kind in (KIND_CUSTOM_FIELD, KIND_CONFLICT) and item.custom_field_id is not None:
            if custom_fields_patch is None:
                custom_fields_patch = [dict(e) for e in survivor.custom_fields_raw]
            custom_fields_patch = [
                e for e in custom_fields_patch if e.get("field") != item.custom_field_id
            ]
            custom_fields_patch.append(
                {"field": item.custom_field_id, "value": item.custom_field_value}
            )
            applied.append(item.label)
        elif item.kind == KIND_NOTE:
            for text in item.note_texts:
                await paperless.add_note(survivor.id, f"From #{loser_id}: {text}")
            applied.append(item.label)

    if tag_ids:
        await paperless.bulk_modify_tags([survivor.id], add_tags=tag_ids)
    if custom_fields_patch is not None:
        update_fields["custom_fields"] = custom_fields_patch
    if update_fields:
        await paperless.update_document(survivor.id, **update_fields)

    return applied


def non_conflict_indices(items: list[DiffItem]) -> set[int]:
    """Indices applied by "Copy all" (SPEC-dups §5.3): every item except
    conflicts."""
    return {idx for idx, item in enumerate(items) if not item.is_conflict}
