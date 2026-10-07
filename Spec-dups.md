# SPEC-dups — Near-duplicate review (`/dups`)

Addition to SPEC.md. Milestone 6. All rules in CLAUDE.md apply (clean-room, MIT, no heavy dependencies, mypy strict, no AVX).

Do not change existing behavior. Add new code only. Update `/help`, `i18n.py` (uk, pl, ru, en), `.env.example` and README.

---

## 1. Goal

The archive has the same document scanned more than once, with different scan quality. Paperless v3 finds only exact duplicates (same checksum). The bot must find near-duplicates and let the owner:
1. choose the copy to keep,
2. select which metadata to copy from the other copy,
3. move the other copy to the Paperless trash.

No LLM is used. No cost.

---

## 2. Configuration (new env)

| Var | Default | Notes |
|---|---|---|
| `DUPS_ENABLED` | `true` | |
| `DUPS_SCAN_TIME` | `03:30` | Local time, daily. Paperless export runs at 03:00 |
| `DUPS_THRESHOLD` | `0.80` | Minimum text similarity |
| `DUPS_NUMBERS_THRESHOLD` | `0.70` | Minimum number-token similarity (see 3.2) |
| `DUPS_MIN_CHARS` | `200` | Documents with less text are not compared |
| `DUPS_SKIP_TAGS` | `gpt-ocr,gpt-auto,sonnet-ocr,sonnet-auto` | A document with one of these tags is waiting for OCR or classification. Do not compare it yet |
| `DUPS_NOTIFY` | `true` | Send one message when new candidate pairs are found |

---

## 3. Candidate detection

### 3.1 Data
- Read all documents (id, title, created, correspondent, tags, page count, content) through the Paperless API. Use paging.
- Read file data with `GET /api/documents/{id}/metadata/` (original size, MIME type, archive size). Cache it.
- Exact duplicates: use `has_duplicates=true`. Add these pairs with similarity `1.0`.

### 3.2 Similarity
1. Normalize the text: Unicode NFKC, lowercase, remove punctuation, collapse spaces.
2. Text similarity: Jaccard on word shingles (start with 3 words; the implementer can change this if tests show a better value). Use MinHash only if the archive grows over about 3000 documents.
3. Number similarity: Jaccard on the set of number tokens (dates, amounts, IDs, tokens with digits).
   - Reason: forms and templates (for example the same tax form for two different years) have almost the same text but different numbers. They are not duplicates.
4. A pair is a candidate when text similarity ≥ `DUPS_THRESHOLD` and number similarity ≥ `DUPS_NUMBERS_THRESHOLD`.
5. Prefilter pairs by text length ratio (skip if the shorter text is below 50% of the longer text) to save time.
6. Cache the shingle sets and number sets in SQLite. Key: doc id + hash of content. Recompute only for new or changed documents.
7. A pair is stored once (`a_id < b_id`). Ignore pairs that the owner marked "not duplicates" (see 6).

### 3.3 Scan schedule
- Daily at `DUPS_SCAN_TIME` (JobQueue). Also on demand: `/dups scan`.
- Skip documents that carry a tag from `DUPS_SKIP_TAGS` or have fewer than `DUPS_MIN_CHARS` characters.
- If the scan finds new pairs and `DUPS_NOTIFY` is true, send one message to each allowed user: `N possible duplicate pairs. Use /dups`.

---

## 4. Commands

| Command | Action |
|---|---|
| `/dups` | Show the next open pair (highest similarity first). If none: "No open pairs." |
| `/dups scan` | Run the scan now. Report the number of new pairs |
| `/dups stats` | Open pairs, resolved pairs, "not duplicates" count, last scan time |
| `/dups undo` | Restore the last trashed loser from the Paperless trash (see 5.5) |

All `callback_data` must be 64 bytes or less. Store the state in SQLite. Use short IDs (`d:<action>:<pair_id>[:<idx>]`).

---

## 5. Review flow

### 5.1 Pair card
One message per pair. Content for each copy (A and B):
- `#id title`, created date, correspondent
- page count, original file size, MIME type
- OCR text length (characters)
- link to Paperless

Also show: text similarity and number similarity (percent).

Warnings (⚠) when:
- page counts differ,
- created dates differ,
- number similarity is below 0.9.

A ⭐ marks the recommended copy. Rule, in order: more OCR characters → larger original file → newer. This is a hint only. The owner decides.

Buttons:
- `📄 A`, `📄 B`: send the original file (same logic as SPEC §4.3)
- `Keep A`, `Keep B`
- `Not duplicates`
- `Skip` (show the next pair, keep this pair open)

### 5.2 Metadata comparison
After `Keep X`, the bot compares the survivor (S) with the loser (L). It builds a list of items. Do not include hidden tags (`HIDDEN_TAG_PREFIXES`).

| Item type | Added to the list when | Action if selected |
|---|---|---|
| tag | L has a tag that S does not have | add the tag to S |
| correspondent | S has none and L has one | set it on S |
| document type | S has none and L has one | set it on S |
| custom field | L has a field that S does not have | add the field with L's value to S |
| note | L has notes | add each note to S as a new note. Prefix: `From #L: ` |
| conflict | both have the same custom field with different values | replace S's value with L's value |

Rules:
- Never overwrite a value of S, except for a selected **conflict** item.
- Title, created date and file always stay from S.
- If the list is empty: go to 5.4 at once.

### 5.3 Selection screen
One message. One line of text per item. One toggle button per item (`☐ tag: contract` / `☑ tag: contract`). Push the button to toggle. Edit the message in place.

- Default state: **all items unchecked**.
- Conflict items are marked ⚠ and show both values (`Expires: S=14.12.2026, L=14.12.2027`).

Bottom buttons:
- `✅ Copy all`: apply all non-conflict items at once. This does NOT apply conflict items.
- `Copy selected (n)`: apply the checked items (including checked conflicts).
- `Skip copy`: apply nothing, go to 5.4.
- `Cancel`: do nothing. The pair stays open. Neither document changes.

### 5.4 Apply order (must follow exactly)
1. Apply the selected metadata to S (PATCH, notes). Read S back from the API and check the result.
2. If step 1 fails: do NOT trash L. Show the error. The pair stays open.
3. Move L to the trash (`DELETE /api/documents/{id}/`). Paperless keeps trashed documents for 30 days.
4. Mark the pair resolved. Remove all other open pairs that contain L.
5. Write an action record to SQLite: pair id, survivor, loser, applied items (JSON), time.
6. Confirm to the owner: `Kept #S. Moved #L to trash. Copied: <list>.`

### 5.5 Undo
`/dups undo` restores the most recent loser from the trash (`TODO(paperless-api)`: verify the trash restore endpoint). The bot shows the list of metadata that it copied to S, so the owner can remove it by hand. Automatic metadata rollback is out of scope.

---

## 6. SQLite (add to the existing DB)

- `dup_doc_cache(doc_id, content_hash, shingles BLOB, numbers BLOB)`
- `dup_pairs(id, a_id, b_id, text_sim, num_sim, status, created_at)`. Status: `open`, `resolved`, `not_dup`
- `dup_sessions(id, pair_id, survivor_id, loser_id, items_json, checked_json, created_at)`. Clean up after 24 hours
- `dup_actions(id, pair_id, survivor_id, loser_id, applied_json, created_at)`

Use a simple migration (version number in a table).

---

## 7. Paperless API points to verify

Mark each point with `TODO(paperless-api)` in code, and add a test. Do not guess silently.
- `has_duplicates=true` filter
- `/api/documents/{id}/metadata/` response fields
- PATCH format for `tags` and `custom_fields` (list of `{field, value}`)
- Notes: create with `POST /api/documents/{id}/notes/`
- `DELETE /api/documents/{id}/` moves the document to the trash (v3)
- Trash restore endpoint
- Monetary field value format when copying fields

---

## 8. Tests

- Similarity: two synthetic noisy copies of one text (uk and pl samples, with OCR-like errors) score ≥ threshold. Two forms with the same text but different numbers do NOT become candidates.
- Skip rules: documents with `DUPS_SKIP_TAGS` or under `DUPS_MIN_CHARS` are not compared.
- Diff builder: every item type, hidden tags excluded, conflict detected, empty list.
- Selection UI: toggles, `Copy all` excludes conflicts, `Copy selected` includes checked conflicts, `callback_data` ≤ 64 bytes.
- Apply order: the loser is not trashed when the metadata step fails.
- "Not duplicates" pairs do not come back after a rescan.
- Pairs that contain a trashed document are removed.
- Use respx for Paperless. No real network.

---

## 9. Done

ruff, mypy --strict and pytest are green. README documents `/dups`, the new env vars and the review flow. The `TODO(paperless-api)` items are listed in README.