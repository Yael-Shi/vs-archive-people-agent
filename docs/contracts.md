# Contracts

This slice consumes versioned JSON and produces review artifacts. It does not write `Person`, `PersonAlias`, `PersonRegistryImportBinding`, `ArchiveItemPerson`, biography text, or `PhotoPerson`.

Identity review and biography review are separate. This repository has no biography package.

## Generic rules carried forward

These rules replace the historical batch policy file. Person-specific identity rules are not part of this contract.

- Evidence comes only from declared displayed text. A title is metadata.
- After whitespace runs collapse to one space, the evidence snippet is a contiguous substring of that displayed body, and the exact mention occurs inside the snippet. Letters and punctuation are not rewritten.
- Every mention receives one run-local `mention_key` of the form `M0001`, assigned in order of `archive_item_id`, `evidence_source`, `start_offset`, and `end_offset`. That key is a label for this run. It is not the durable identity of the mention.
- `mention_fingerprint` is the durable identity. It is the SHA-256 of the canonical JSON object whose fields are `archive_item_id`, `evidence_source`, `source_text_sha256` (lowercase), `start_offset`, and `end_offset`. `occurrence_index` and the proposal's spelling of the mention are not part of the digest. A caller-supplied fingerprint or `mention_key` is discarded. The fingerprint does not approve identity.
- `occurrence_index` is display order only. `freeze-evidence` assigns it from source offsets inside each `(archive_item_id, evidence_source)`. Caller numbering is ignored. An earlier span keeps its fingerprint when a later finder reports a span before it.
- One resolution exists per `mention_key`.
- A pending or local candidate is not production identity.
- An existing `PersonAlias` is a production name match. An inferred alias is a separate review flag and is not created here.
- `MERGE` and `CANONICAL_NAME_CHANGE` are recorded only when a mention proposal already carries that action.
- Confidence is stored and is not used to choose a class or to approve a row.
- Artifact publication is atomic and refuses an existing path. A complete temporary file is linked onto the destination. That link fails when the path already exists, so another publisher cannot replace it.
- A run moves only `discover -> freeze-evidence -> resolve -> review`. That order is the Python state-machine sequence. The checkpoint schema enum lists the allowed step names and does not define their order. A later checkpoint is a new file. Every previously recorded input hash must be presented again and must match. The checkpoint keeps the schema versions it started with.

## document-text-snapshot-v1

File: `src/vs_archive_people/schemas/document_text_snapshot.v1.json`.

`vs-archive` exports this. `people-agent` reads it.

Required item fields are `archive_item_id` and `item_type` (`OCR_DOCUMENT` or `MANUAL_TEXT`). Displayed evidence paths are:

- `ocr_document.displayed_transcription.text`
- `ocr_document.displayed_source_text.text`
- `ocr_document.displayed_hebrew_translation.text`
- `manual_text.body`

`title`, `created_at`, and `updated_at` may be present. Discovery does not select an item because a timestamp changed.

The displayed-text hash is the SHA-256 of the canonical JSON array of selected bodies, in role order, after identical raw strings have been merged into one body with multiple roles. Whitespace-only differences stay separate.

## identity-snapshot-v1

File: `src/vs_archive_people/schemas/identity_snapshot.v1.json`.

This is the production identity export. Required model groups are `Person`, `PersonAlias`, and `PersonRegistryImportBinding`. `ArchiveItemPerson` and `PhotoPerson` may be present on a full export.

Resolution indexes:

- `Person.id` and `Person.name`
- `PersonAlias.id`, `PersonAlias.person_id`, and `PersonAlias.name`
- `PersonRegistryImportBinding.person_id` and `stable_key`, for display on a match

`stable_key` is not compared to mention text. `Person.biography` is not copied. Rows with `review_status` `PENDING` are excluded from the production index. `PhotoPerson` rows are counted as ignored and are never emitted. `ArchiveItemPerson` is not a name index in this slice.

Extra export fields may remain on a row. The resolver still fails closed when an invariant it depends on is broken: duplicate `Person.id`, duplicate `PersonAlias.id`, duplicate binding id, an alias or binding that points at a Person that is not in the production index, a blank name or a name that normalizes to nothing, two alias ids for one person and one normalized name, or one `stable_key` bound to two people. Identical repeated bindings for the same person and key are one binding.

When a person's canonical name and alias normalize to the same string, the match stays `matched_via = canonical_name`. The alias id is retained for lookup and is not a second person. When that same normalized alias belongs to more than one production person, the mention is `AMBIGUOUS`. One person may have several stable keys. They are stored in sorted order and shown together on a match. Shared names are not collapsed.

A deterministic match is an exact normalized name hit on exactly one production person. Normalization is NFKC, casefold, and whitespace collapse. Titles are not stripped, and names are not fuzzy-matched. Row order in the export does not change the index.

## discovery-selection-v1

File: `src/vs_archive_people/schemas/discovery_selection.v1.json`.

Written by `discover`. A later evidence package binds it by logical content hash and also records the discovery file hash.

`document_snapshot_file_sha256` and each `displayed_text_sha256` are 64-character hexadecimal digests. `prior_checkpoint_logical_sha256` and `prior_checkpoint_file_sha256` are the same shape, or null when there is no prior checkpoint. `reason` is `not_previously_processed` or `displayed_text_changed`.

An item is selected when its id is absent from the prior checkpoint `processed_items`, or when the stored displayed-text hash differs. A prior checkpoint is previous-run memory. Its `run_id` does not have to equal the new discovery run.

## mention-proposals-v1

File: `src/vs_archive_people/schemas/mention_proposals.v1.json`.

Written by `package-mention-proposals` or `find-mentions`. This is a generated artifact. It uses the same provenance, logical-hash, and atomic no-clobber publication rules as the other generated artifacts. v1 `freeze-evidence` does not read this file. The v1 loose proposal list remains that command's input. Official proposals must not be fed to v1 freeze. `freeze-evidence-v2` is the consumer. See evidence-package-v2 below.

Required top-level fields:

- `schema_version` — `mention-proposals-v1`
- `run_id` — equal to the discovery selection `run_id`
- `artifact_created_at`, `producer`, `logical_content_sha256`
- `document_snapshot_file_sha256` — audit hash of the snapshot file. Outside the logical hash
- `discovery_logical_sha256` — semantic binding to discovery. Inside the logical hash
- `discovery_file_sha256` — audit hash. Outside the logical hash
- `identity_snapshot_file_sha256` — audit hash, or null. The human packager writes null because it does not read an identity snapshot. Outside the logical hash
- `production_write_performed` — false
- `detectors` — registry. May be empty only when `mentions` is empty
- `mentions` — person-candidate rows. May be empty

There is no `diagnostics` field. Undeclared fields are rejected.

Each detector has non-empty `id`, `role`, `implementation`, and `version`. `id` values are unique in the file. `role` is any non-empty string. Optional `reproducibility` is an object whose values are non-empty strings. The human packager writes one detector: `id` `human-authored`, `role` `human`, `implementation` `manual`, `version` `1`. `find-mentions` writes two detectors: `semantic-surface` (`role` `semantic`, `implementation` `gemini-3.8-flash`, `version` `1`, `reproducibility.prompt_sha256`) and `known-name` (`role` `deterministic`, `implementation` `exact-literal`, `version` `1`). Mention rows reference those ids. They do not carry a provider payload.

Each mention has `archive_item_id`, `evidence_source`, `exact_mention`, `start_offset`, `end_offset`, `evidence_snippet`, `name_surface`, `name_start_offset`, `name_end_offset`, `title_surface`, `title_start_offset`, `title_end_offset`, `surface_kind`, `detector_ids`, and `known_name_match_quality`.

`surface_kind` is `FULL_NAME`, `PARTIAL_NAME`, `TITLE_AND_NAME`, `NICKNAME`, `UNCERTAIN_PERSON_NAME`, or null. `TITLE_AND_NAME` requires a title slice. A title slice allows `TITLE_AND_NAME` or null, and rejects the other kinds. A row with no title rejects `TITLE_AND_NAME`. Null remains valid for a human-authored row. `known_name_match_quality` is `EXACT_BOUNDARY`, `PREFIXED`, or null, and it is non-null exactly when `detector_ids` contains `known-name`. Human-authored rows use null. `detector_ids` is a non-empty set of ids declared in `detectors`.

The outer span and the name span are non-empty. The name span lies inside the outer span. Title offsets are both null, with `title_surface` null, or both integers for a non-empty title inside the outer span that does not overlap the name. `exact_mention`, `name_surface`, and `title_surface` are the slices at those offsets. An outer span may contain at most one `\n`. `\r\n` is that one line break and stays exact. Two or more `\n` characters are rejected, including a blank line and a span that crosses a middle line. Whitespace, the original line break, and a safe Hebrew proclitic may sit outside the slices. Unicode punctuation and separators may sit between the title and the name. Letters or digits in that gap are rejected. A peel of only `ה` or `ש` is rejected.

Generated mention rows are ordered by `archive_item_id`, `evidence_source`, `start_offset`, and `end_offset` before the logical hash is stamped. That source order is part of the published artifact. The global logical-hash exclusion rule is unchanged. A duplicate `(archive_item_id, evidence_source, start_offset, end_offset)` is rejected by artifact validation for every producer.

The artifact does not carry `person_id`, `mention_key`, `mention_fingerprint`, `occurrence_index`, `mention_class`, `propose_alias`, `proposed_identity_action`, `confidence`, or `detector_attributes`.

`package-mention-proposals` accepts plain JSON, not a generated artifact:

```text
package-mention-proposals \
  --documents data/document_text_snapshot.json \
  --discovery runs/discovery.json \
  --geometry data/mention_geometry.json \
  --run-id run-001 \
  --output runs/mention-proposals.json
```

`--geometry` is an object with a `mentions` array. Each item allows only `archive_item_id`, `evidence_source`, `start_offset`, `end_offset`, `name_start_offset`, `name_end_offset`, `title_start_offset`, `title_end_offset`, and optional `surface_kind`. Any other property is refused, including `exact_mention`, `evidence_snippet`, and identity fields. Omitted `surface_kind` becomes null. Title offsets are both null or both integers.

The command verifies the discovery artifact and its logical hash, requires the same `run_id`, and requires every mentioned item to be in the discovery selection. It resolves the declared displayed body and copies slices from that unmodified text. Python code-point offsets are preserved. A span may stay on one line or cross one adjacent line break, including `CRLF`. `evidence_snippet` is the exact source line window that covers the outer offsets. The geometry file cannot supply it. Published mentions are sorted into source order before stamping. Artifact validation refuses a duplicate outer span.

Success prints `status`, the output file hash, the logical hash, `schema_version`, and the mention count. An existing destination fails. A blocked command leaves no partial destination.

`find-mentions` reads the document-text snapshot, the discovery selection, and the identity snapshot. It requires the same `run_id` as discovery. It walks every selected displayed body, then publishes one proposal for the complete selection. A body with zero mentions is success. A required semantic-provider failure before publication publishes no proposal and does not attempt the sidecar. Semantic requests split at 20,000 code points of segment text, and each later batch repeats the previous batch's last segment so a one-line cross-line mention at that boundary is visible. Identical outer spans from that overlap become one proposal row. A provider failure on a later batch still publishes nothing.

```text
find-mentions \
  --documents data/document_text_snapshot.json \
  --discovery runs/discovery.json \
  --identity data/identity_snapshot.json \
  --run-id run-001 \
  --output runs/mention-proposals.json \
  --diagnostics runs/mention-proposal-diagnostics.json
```

The semantic detector proposes source surfaces. Local code aligns them to Python code-point offsets in the unmodified body. The known-name detector searches accepted `Person.name` and accepted `PersonAlias.name` as exact literals. It publishes `EXACT_BOUNDARY` and a safe `PREFIXED` proclitic peel. `EMBEDDED` is diagnostic only. Neither detector assigns `person_id`.

Consolidation is per archive item and evidence source. The same outer span merges `detector_ids`. Two exact splits of that span publish neither row and record `SPLIT_CONFLICT`. Exactly one titled outer absorbs an inner span at its name offsets. Several titled outers keep every valid span and record `ABSORB_AMBIGUOUS`. Other overlapping spans stay. Published rows are ordered by archive item, evidence source, and outer offsets.

## mention-proposal-diagnostics-v1

File: `src/vs_archive_people/schemas/mention_proposal_diagnostics.v1.json`.

Written by `find-mentions` after `mention-proposals-v1` is published. This is a generated artifact with its own provenance and logical hash. It is not an input to freeze, resolve, or review. Changing it cannot change the proposal logical hash.

Required fields:

- `schema_version` — `mention-proposal-diagnostics-v1`
- `run_id` — the same run as the proposal
- `artifact_created_at`, `producer`, `logical_content_sha256`
- `mention_proposals_logical_sha256` — semantic parent. Inside the logical hash
- `mention_proposals_file_sha256` — audit hash of the published proposal. Outside the logical hash
- `rejected`, `suppressed`, `conflicts`

Each row has `archive_item_id`, `evidence_source`, `surface`, `reason`, and `segment_id` (or null). Offsets are present only after a local slice check succeeded. Conflict rows always have offsets.

`rejected` reasons: `UNKNOWN_SEGMENT`, `EMPTY_SURFACE`, `SURFACE_NOT_IN_SEGMENT`, `END_SEGMENT_NOT_NEXT_LINE`, `AMBIGUOUS_REPEATED_SURFACE`, `OCCURRENCE_OUT_OF_RANGE`, `OFFSET_INVARIANT_FAILED`, `TITLE_SPLIT_UNRESOLVED`, `UNSAFE_NAME_SPLIT`, `NAME_SURFACE_NOT_IN_FULL`, `AMBIGUOUS_NAME_SURFACE`, `TITLE_SURFACE_NOT_IN_FULL`, `AMBIGUOUS_TITLE_SURFACE`, `TITLE_NAME_OVERLAP`.

`suppressed` reasons: `TITLE_OR_ROLE_WITHOUT_NAME`, `PLACE_OR_INSTITUTION_CONTEXT`, `EMBEDDED_KNOWN_NAME`.

`conflicts` reasons: `SPLIT_CONFLICT`, `ABSORB_AMBIGUOUS`.

The sidecar does not store raw provider JSON, prompt text, an API key, token counts, or response bodies. There is no two-file transaction. If the proposal link succeeds and the sidecar link fails, the proposal stays. Stdout is `status` `PROPOSAL_PUBLISHED`, `error_code` `DIAGNOSTICS_PUBLICATION_FAILED`, the proposal path, both proposal hashes, and `diagnostics_published` false. The process exits nonzero. An existing proposal destination fails before publication and writes no sidecar.

## evidence-package-v1

File: `src/vs_archive_people/schemas/evidence_package.v1.json`.

Built only for the discovery selection. `freeze-evidence` requires `discovery.run_id` to equal the requested `run_id`. Items are ordered by `archive_item_id`. Mentions are ordered by `archive_item_id`, `evidence_source`, `start_offset`, and `end_offset`. Each mention includes `mention_key`, `mention_fingerprint`, `archive_item_id`, `text_role`, `evidence_source`, `source_text_sha256`, `exact_mention`, `evidence_snippet`, `start_offset`, `end_offset`, and `occurrence_index`. `review_status` stays `PENDING`. `production_write_performed` is false.

The package records `document_snapshot_file_sha256`, `discovery_logical_sha256`, `discovery_file_sha256`, and `mention_proposals_file_sha256`. The logical field is copied from the verified discovery artifact. The file fields are audit hashes of the snapshot file, the discovery file, and the mention-proposals file. `mention_proposals_file_sha256` does not replace the frozen mentions stored in the package. `freeze-evidence` refuses a discovery file whose document file hash is not the hash of the snapshot just read, and refuses a selected item whose displayed-text hash no longer matches. Offsets are zero-based Python code-point indexes into the exact displayed body. `body_text[start_offset:end_offset]` must equal `exact_mention` with no whitespace normalization. The snippet check may still collapse whitespace. A duplicate fingerprint is refused. A duplicate source span for one item and text role is refused.

Mention finding is not this step. `freeze-evidence` only checks the loose proposal list and publishes the evidence package. It does not read `mention-proposals-v1`. A file whose `schema_version` is `mention-proposals-v1` is `UNSUPPORTED_SCHEMA_VERSION`.

The v1 freeze input remains a JSON list, or an object with a `mentions` array:

```text
archive_item_id
evidence_source
start_offset
end_offset
exact_mention
evidence_snippet
mention_class              optional, PERSON | NOT_A_PERSON
propose_alias              optional boolean
proposed_identity_action   optional, MERGE | CANONICAL_NAME_CHANGE
confidence                 optional integer 0..100
```

`start_offset` and `end_offset` are zero-based offsets into the displayed body named by `evidence_source`. `freeze-evidence` is the consumer of those records. It assigns `mention_key`, `mention_fingerprint`, and `occurrence_index`. A caller `occurrence_index`, `mention_key`, or `mention_fingerprint` is not identity. The finder must not assign production identity, approve a new Person, or write production data. `find-mentions` publishes `mention-proposals-v1` and does not feed that file to this command. The human command `package-mention-proposals` publishes the same official file from geometry. That official file is not a v1 freeze input.

## evidence-package-v2

File: `src/vs_archive_people/schemas/evidence_package.v2.json`.

Written by `freeze-evidence-v2`. This command is outside `RUN_STEPS`. It does not replace v1 freeze. Inputs are the document-text snapshot, the discovery selection, `mention-proposals-v1`, the run id, and the output path. There is no diagnostics argument, no loose proposal list, and no identity snapshot. Passing a loose v1 proposal list is `UNSUPPORTED_SCHEMA_VERSION` when the value is an object, and `MALFORMED_ARTIFACT` when it is a list. There is no automatic migration.

```text
freeze-evidence-v2 \
  --documents data/document_text_snapshot.json \
  --discovery runs/discovery.json \
  --mentions runs/mention-proposals.json \
  --run-id run-001 \
  --output runs/evidence-v2.json
```

The package is a generated artifact. Logical parents are `discovery_logical_sha256` and `mention_proposals_logical_sha256`. Audit-only physical hashes are `document_snapshot_file_sha256`, `discovery_file_sha256`, and `mention_proposals_file_sha256`. Those three names end in `_file_sha256`, so the existing logical-hash exclusion rule leaves them out of the package logical body. `identity_snapshot_file_sha256` on the proposal is not copied.

Before mentions are frozen, the command requires a generated `mention-proposals-v1` artifact, recomputes its logical hash, requires `run_id` to equal the active run, requires the proposal's document snapshot file hash to equal the snapshot file supplied to freeze, and requires the proposal's discovery logical hash and discovery file hash to equal the verified discovery artifact and the discovery file supplied to freeze. A wrong schema is `UNSUPPORTED_SCHEMA_VERSION`. A logical-hash mismatch on the proposal or on discovery is `LOGICAL_HASH_MISMATCH`. A run-id mismatch is `RUN_ID_MISMATCH`. A parent hash that does not match the files supplied to this command is `INPUT_HASH_CHANGED`.

Discovery follows the v1 source-selection rules. The discovery artifact must be valid, share `run_id`, and be bound to this document snapshot file. Every proposal row must belong to a selected item. Every selected item's `displayed_text_sha256` must still match. `evidence_source` must name a displayed role for that item. A proposal outside the selection is rejected.

For every mention, freeze re-slices the unmodified displayed body:

- `body[start_offset:end_offset]` equals `exact_mention`
- `body[name_start_offset:name_end_offset]` equals `name_surface`
- when a title is present, `body[title_start_offset:title_end_offset]` equals `title_surface`

It also requires a non-empty outer span, a non-empty name span inside that outer span, title offsets that are both null or both integers, a title span inside the outer span, no overlap between title and name, at most one line break in the outer span, and split residue that satisfies the official geometry rule. `surface_kind` must agree with title presence. Detector ids must be declared in the proposal registry. `known_name_match_quality` is non-null exactly when `known-name` contributed, and then only `EXACT_BOUNDARY` or `PREFIXED`. These checks run again at freeze. A proposal that validated when it was written is not trusted for source geometry. A duplicate `(archive_item_id, evidence_source, start_offset, end_offset)` fails closed. Rows are not silently deduplicated.

Each evidence row stores `mention_key`, `mention_fingerprint`, `occurrence_index`, `source_text_sha256`, and the verified proposal geometry: `archive_item_id`, `evidence_source`, `exact_mention`, `start_offset`, `end_offset`, `evidence_snippet`, `name_surface`, `name_start_offset`, `name_end_offset`, `title_surface`, `title_start_offset`, `title_end_offset`, `surface_kind`, `detector_ids`, and `known_name_match_quality`. `source_text_sha256` is the displayed-body hash already used by the v1 fingerprint. The row does not store the detector registry, provider metadata, `person_id`, `mention_class`, `propose_alias`, `proposed_identity_action`, or confidence.

Occurrence identity is unchanged from v1. `mention_fingerprint` is the SHA-256 of canonical JSON for `archive_item_id`, `evidence_source`, `source_text_sha256` (lowercase), `start_offset`, and `end_offset`. It does not include `name_surface`, title text, `detector_ids`, or model metadata. `mention_key` is assigned in order of archive item, evidence source, and outer offsets. `occurrence_index` is display order inside each `(archive_item_id, evidence_source)` from those offsets. Equivalent proposal rows in a different JSON order produce the same evidence mention rows. The proposal logical hash still follows the supplied artifact, so two proposal files that differ only by mention order have different parent hashes and different package logical hashes even when the mention rows match. Producers that sort before stamping, as `package-mention-proposals` does, produce equivalent package logical hashes.

`production_write_performed` is false. Selected items are stored in the same shape as v1. v1 `resolve`, review, and `run-checkpoint-v1` do not read this package. `resolve-v2` is the consumer. See resolution-report-v2 below.

## resolution-report-v1

File: `src/vs_archive_people/schemas/resolution_report.v1.json`.

Classifications:

- `DETERMINISTIC_EXISTING_PERSON` — exactly one production person
- `AMBIGUOUS` — more than one production person
- `NEW_PERSON_CANDIDATE` — no production person
- `NOT_A_PERSON` — the mention proposal set `mention_class` to `NOT_A_PERSON`

Resolution copies `run_id` from the evidence package. Each resolution copies `evidence_source`, `source_text_sha256`, `evidence_snippet`, `start_offset`, `end_offset`, `occurrence_index`, and `mention_fingerprint` from the mention so later review does not need to reopen the evidence package. The report records `evidence_package_logical_sha256` from the verified package and `evidence_package_file_sha256` plus `identity_snapshot_file_sha256` as audit file hashes. `local_candidates_file_sha256` is the audit hash of `--local-candidates` when that file is supplied, and null when it is not. The builder rejects a candidate list without that hash, including an empty list, and rejects a hash when no list was supplied. Resolutions are ordered by `mention_key`.

`confidence_approves_identity` and `local_candidates_are_production_identity` are false. `photo_person_created` is false.

`inferred_alias_proposal` is true only when the proposal sets `propose_alias` and the normalized mention is not already that person's canonical name or alias. A unique alias hit is `matched_via = person_alias` and is not an inferred alias.

## resolution-report-v2

File: `src/vs_archive_people/schemas/resolution_report.v2.json`.

Written by `resolve-v2`. This command is outside `RUN_STEPS`. It does not replace v1 `resolve`. Inputs are `evidence-package-v2`, the identity snapshot, the run id, and the output path. There is no local-candidates argument and no automatic migration from v1 evidence.

```text
resolve-v2 \
  --evidence runs/evidence-v2.json \
  --identity data/identity_snapshot.json \
  --run-id run-001 \
  --output runs/resolution-v2.json
```

The lookup string is `name_surface`. `exact_mention` is copied and is not matched. Resolve does not re-slice source text, strip a title, or peel a Hebrew proclitic. A change that touches only `title_surface` does not change the lookup string. Passing `evidence-package-v1` is `UNSUPPORTED_SCHEMA_VERSION`. v1 `resolve` still requires `evidence-package-v1` and still fails closed on `evidence-package-v2`.

The package must be a generated `evidence-package-v2` artifact. Resolve recomputes its logical hash and requires `run_id` to equal the active run. A logical-hash mismatch is `LOGICAL_HASH_MISMATCH`. A run-id mismatch is `RUN_ID_MISMATCH`. A schema failure inside that version is `MALFORMED_ARTIFACT`.

Classifications:

- `DETERMINISTIC_EXISTING_PERSON` — `name_surface` hits exactly one production person
- `AMBIGUOUS` — more than one production person
- `NEW_PERSON_CANDIDATE` — no production person

`NOT_A_PERSON` is not a v2 classification. The schema does not list it. Detector fields do not choose the class. `known-name` provenance does not assign `person_id`.

Each resolution copies `mention_key`, `mention_fingerprint`, `archive_item_id`, `evidence_source`, `source_text_sha256`, `exact_mention`, `start_offset`, `end_offset`, `evidence_snippet`, `occurrence_index`, `name_surface`, `name_start_offset`, `name_end_offset`, `title_surface`, `title_start_offset`, `title_end_offset`, `surface_kind`, `detector_ids`, and `known_name_match_quality` from the evidence row. It also stores `matched_person_ids`, `matched_person_name`, `matched_via`, `matched_alias_id`, `stable_keys`, `review_status` `PENDING`, and `identity_is_production`. A unique match stores that person's canonical name and stable keys. Several matches store every matched person id, in index order, and the sorted stable keys. `matched_person_name` is null unless the match is unique. `identity_is_production` is true only for `DETERMINISTIC_EXISTING_PERSON`.

Rows are ordered by `mention_key`. Shuffling the evidence mention array does not change that row order. The evidence logical hash still follows the supplied artifact, so a different mention order is a different parent and a different report logical hash even when the resolution rows match.

Logical parent: `evidence_package_logical_sha256`. Audit-only physical hashes: `evidence_package_file_sha256` and `identity_snapshot_file_sha256`. Both names end in `_file_sha256`, so the existing logical-hash exclusion rule leaves them out of the report logical body. The identity snapshot is an external file, not a generated logical parent. There is no `local_candidates_file_sha256`. v1 local candidates only flagged same-run overlap for review. They are not production identity, and this phase has no review v2 consumer, so v2 does not invent that input.

`production_write_performed` and `photo_person_created` are false. The command reads the identity snapshot and publishes the report. It does not create a Person, alias, merge, or canonical-name change, and it does not approve identity. `review-package` still reads only `resolution-report-v1`. `review-v2` is the consumer of this report. See identity-review-package-v2 below.

## identity-review-package-v1

File: `src/vs_archive_people/schemas/identity_review_package.v1.json`.

`kind` is `IDENTITY_REVIEW_NOT_APPLY`. This is not an apply package.

`review_rows` contains every ambiguous match, every new-person candidate, every inferred alias proposal, and every explicit identity-changing action.

`auto_resolution_proposals` lists deterministic matches with `proposal_status` `PROPOSED_NOT_APPLIED`. They are audit proposals. They are not approved writes.

Review copies `run_id` from the resolution report. Both lists carry `evidence_source`, `source_text_sha256`, `evidence_snippet`, `start_offset`, `end_offset`, `occurrence_index`, and `mention_fingerprint` from the resolution row. The package records `resolution_report_logical_sha256` from the verified report and `resolution_report_file_sha256` as the audit file hash. A reviewer can decide from this package alone. Each list is ordered by `mention_key`.

No biography field is present. No `PhotoPerson` row is present.

## identity-review-package-v2

File: `src/vs_archive_people/schemas/identity_review_package.v2.json`.

Written by `review-v2`. This command is outside `RUN_STEPS`. It does not replace v1 `review-package`. The input is a verified `resolution-report-v2`, the run id, and the output path. There is no evidence argument, no identity snapshot, no diagnostics argument, no local-candidates argument, and no decision file. Passing `resolution-report-v1` is `UNSUPPORTED_SCHEMA_VERSION`. v1 `review-package` still requires `resolution-report-v1` and raises `UNSUPPORTED_SCHEMA_VERSION` on `resolution-report-v2`. There is no automatic migration.

```text
review-v2 \
  --resolution runs/resolution-v2.json \
  --run-id run-001 \
  --output runs/identity-review-v2.json
```

The package is a generated artifact. The logical parent is `resolution_report_logical_sha256`. The audit-only physical hash is `resolution_report_file_sha256`. That name ends in `_file_sha256`, so the existing logical-hash exclusion rule leaves it out of the package logical body. Review does not reopen the evidence package, the identity snapshot, or the displayed text, and it does not classify the mention again.

`kind` is `IDENTITY_REVIEW_NOT_APPLY`. `production_write_performed`, `photo_person_created`, `biography_fields_included`, `human_approval_performed`, and `apply_authorized` are false. There is no `auto_resolution_proposals` list and no `proposal_status`.

Every resolution becomes one `review_rows` entry. `DETERMINISTIC_EXISTING_PERSON` stays in that queue. Each row copies the resolution geometry, detector provenance, match fields, and `identity_is_production`, then sets `review_status` to `PENDING` and exactly one `review_reason`:

- `DETERMINISTIC_EXISTING_PERSON` → `deterministic_existing_person_pending_review`
- `AMBIGUOUS` → `ambiguous_existing_person`
- `NEW_PERSON_CANDIDATE` → `new_person_candidate`

`identity_is_production` means the resolution named an existing production person. It does not record human approval. Detector provenance does not drop a row, change the reason, or authorize apply. `NOT_A_PERSON` is not a v2 review classification.

Rows are ordered by `mention_key`. A duplicate `mention_key` or `mention_fingerprint` fails closed. A verified row whose match fields disagree with its classification fails closed and is not repaired: a deterministic row has `identity_is_production` true, exactly one person id, a person name, and `matched_via` of `canonical_name` or `person_alias`; an ambiguous row has `identity_is_production` false, at least two person ids, and null name, via, and alias id; a new-person row has `identity_is_production` false, no person ids, and those same three fields null. Source `review_status` must already be `PENDING`.

An empty resolution list publishes an empty `review_rows` array. The safety flags stay false.

## run-checkpoint-v1

File: `src/vs_archive_people/schemas/run_checkpoint.v1.json`.

Records `run_id`, the schema version map, `input_file_hashes`, completed steps, `output_file_hashes`, and `processed_items`. Those two maps are physical file hashes and are part of the checkpoint logical content. A checkpoint is the exact-file record of a run step.

The only legal step order is `discover -> freeze-evidence -> resolve -> review`. Python owns that progression. The schema enum is the set of allowed serialized step names. A new run can start only at `discover`. The next checkpoint can add only the immediate next step. Duplicate or skipped histories are refused. Repeated checkpoint `--input` or `--record` names are refused, including when both paths have the same digest.

`checkpoint` writes a new file. It does not replace a previous checkpoint. Every input name already stored on the run must be supplied again, and the digest must match. The new checkpoint copies `schema_versions` from the run that started it. This package can resume a checkpoint only when that map equals the versions in the installed `run_checkpoint.v1.json`. A different checkpoint schema version, or a different recorded map, is `UNSUPPORTED_SCHEMA_VERSION`. That is not treated as a corrupt file, and it is not migrated.

SHA-256 fields are 64 hexadecimal characters. Uppercase and lowercase are both accepted. Checkpoints store them in lowercase. `processed_items` are ordered by `archive_item_id`. The same id with two displayed-text hashes is refused.

`processed_items` may be recorded from the evidence package at `freeze-evidence` via `--processed-from`. A partial checkpoint remains resumable for that same run. Only a checkpoint whose `completed_steps` equal the full Python sequence `discover`, `freeze-evidence`, `resolve`, `review` may be `--prior-checkpoint` for a new discovery run. An earlier checkpoint is `INCOMPLETE_PRIOR_CHECKPOINT`, not a malformed artifact. That completed checkpoint may belong to another `run_id`.

Published artifacts are checked with Draft 2020-12 against the schema documents in the package. Each schema document is itself checked as a Draft 2020-12 schema. A document that names a known `schema_version` and then fails that schema is refused. The validation message includes the failing instance path.

`checkpoint` continuation stays on the checkpoint's `run_id` and on `run-checkpoint-v1`. The command refuses a `--run-id` that differs. `--processed-from` refuses an evidence package whose `run_id` differs from `--run-id`, and it refuses an evidence package from the other generation. Discovery may read a review-complete `run-checkpoint-v1` or `run-checkpoint-v2` from another run. That read is memory for a new run. It does not resume the prior generation.

## run-checkpoint-v2

File: `src/vs_archive_people/schemas/run_checkpoint.v2.json`.

Written by `checkpoint-v2`. The logical steps remain `discover -> freeze-evidence -> resolve -> review`. There is no `freeze-evidence-v2`, `resolve-v2`, or `review-v2` checkpoint step name. Generation is the checkpoint `schema_version` and its `schema_versions` map.

The v2 map keeps the v1 field names. `discovery_selection` stays `discovery-selection-v1`. `document_text_snapshot` and `identity_snapshot` stay on their v1 snapshot schemas. `evidence_package` is `evidence-package-v2`. `resolution_report` is `resolution-report-v2`. `identity_review_package` is `identity-review-package-v2`. `run_checkpoint` is `run-checkpoint-v2`. Mention proposals stay outside the map, as they do for v1, because they are not a `RUN_STEPS` stage.

`published_schema_versions()` with no argument remains the v1 map. `published_schema_versions("run-checkpoint-v2")` returns the v2 map. A recorded map that differs from the installed map for that generation is `UNSUPPORTED_SCHEMA_VERSION`.

Same-run resume passes the generation explicitly. `checkpoint` resumes only `run-checkpoint-v1`. `checkpoint-v2` resumes only `run-checkpoint-v2`. Supplying the other generation is `UNSUPPORTED_SCHEMA_VERSION`. The prior checkpoint object is not rewritten into the other schema.

On `freeze-evidence`, `resolve`, and `review`, `checkpoint-v2` reads the recorded artifact and requires its verified schema to be the v2 contract for that role. A v1 artifact is `UNSUPPORTED_SCHEMA_VERSION`. v1 `checkpoint` still allows a hash-only record. When a file is supplied, that file must match the v1 contract. There is no migration.

When `checkpoint-v2` `freeze-evidence` is given `--processed-from`, that file's SHA-256 must equal the SHA-256 recorded for `evidence_package`. The recorded evidence package is still schema-checked, logically hashed, and run-id checked before that comparison. A different file is `INPUT_HASH_CHANGED`. v1 does not add this equality check.

A completed checkpoint of either generation may be `--prior-checkpoint` for discovery when `completed_steps` equals `RUN_STEPS` in order. An earlier checkpoint of either generation is `INCOMPLETE_PRIOR_CHECKPOINT`. The prior `run_id` may differ. The new discovery run keeps the `run_id` it was given. It does not inherit the prior generation.

Completing `review` on a v2 checkpoint means `identity-review-package-v2` was published. Every row in that package stays pending. The checkpoint does not store human approval, apply authorization, or a production write.

Provenance, the logical hash, and atomic no-clobber publication are the same rules as `run-checkpoint-v1`.

Contract failures use these `error_code` values: `MALFORMED_ARTIFACT`, `ARTIFACT_READ_ERROR`, `ARTIFACT_EXISTS`, `INPUT_HASH_CHANGED`, `MISSING_BOUND_INPUT`, `UNSUPPORTED_SCHEMA_VERSION`, `LOGICAL_HASH_MISMATCH`, `RUN_ID_MISMATCH`, `INCOMPLETE_PRIOR_CHECKPOINT`, `FIND_MENTIONS_CONFIGURATION`, `FIND_MENTIONS_PROVIDER`, and `DIAGNOSTICS_PUBLICATION_FAILED`. Invalid JSON is `MALFORMED_ARTIFACT`. A missing, unreadable, or non-UTF-8 input file is `ARTIFACT_READ_ERROR`. `DIAGNOSTICS_PUBLICATION_FAILED` is not a `BLOCKED` result: the proposal file remains, and stdout reports `PROPOSAL_PUBLISHED`.

## Provenance and hashes

Generated artifacts are discovery selections, mention proposals, mention-proposal diagnostics, evidence packages (`evidence-package-v1` and `evidence-package-v2`), resolution reports (`resolution-report-v1` and `resolution-report-v2`), identity review packages (`identity-review-package-v1` and `identity-review-package-v2`), and run checkpoints (`run-checkpoint-v1` and `run-checkpoint-v2`). Each records:

- `artifact_created_at` — UTC time `YYYY-MM-DDThh:mm:ssZ`
- `producer.name` — `vs-archive-people`
- `producer.version` — the package version
- `run_id`
- `logical_content_sha256`

`producer` is `src/vs_archive_people/schemas/producer.v1.json`. `name` and `version` are required. `git_commit` is optional: a non-empty hexadecimal Git object id of 40 to 64 characters. Omission before the first commit is valid. This repository has no committed revision, so the stamp does not write `git_commit`.

`artifact_created_at` is a real UTC datetime serialized as `YYYY-MM-DDTHH:MM:SSZ`. Impossible calendar values are refused. The check is not a digit pattern alone.

`logical_content_sha256` is the SHA-256 of canonical JSON (`sort_keys`, compact separators, UTF-8) after dropping publication metadata and audit-only file hashes. Excluded keys are `artifact_created_at`, `producer`, `logical_content_sha256`, and any top-level key ending in `_file_sha256`. Keys ending in `_logical_sha256` stay in the logical body. Rebuilding a parent at a new timestamp changes its file hash and leaves its logical hash unchanged. A child that binds the parent logical hash therefore keeps the same logical hash and can still store the two different parent file hashes for audit.

Consumers of generated artifacts validate the schema, recompute that logical hash, and refuse a mismatch as `LOGICAL_HASH_MISMATCH`. That check runs when freeze-evidence reads discovery, when `freeze-evidence-v2` reads discovery and `mention-proposals-v1`, when `package-mention-proposals` reads discovery, when v1 resolution reads an evidence package, when `resolve-v2` reads `evidence-package-v2`, when `review-package` reads `resolution-report-v1`, when `review-v2` reads `resolution-report-v2`, and when discovery or checkpoint continuation reads a run checkpoint. External `document-text-snapshot-v1` and `identity-snapshot-v1` files are not generated artifacts and are bound only by file hash. `mention-proposals-v1` is validated when it is published. No later v1 step reads it. `freeze-evidence-v2` is its freeze consumer, and it is outside `RUN_STEPS`. `run-checkpoint-v1` records `evidence-package-v1`, `resolution-report-v1`, and `identity-review-package-v1`. `run-checkpoint-v2` records `evidence-package-v2`, `resolution-report-v2`, and `identity-review-package-v2`. Mention proposals stay outside both maps. `checkpoint` resumes only the v1 map. `checkpoint-v2` resumes only the v2 map.

The physical file hash is the SHA-256 of the indented JSON bytes that were published. `write_json_atomic` returns that hash. It changes when the timestamp or producer version changes, even when the logical hash does not.

Displayed-text hashes and `source_text_sha256` are logical hashes of the displayed body, not file hashes.

## What returns to vs-archive later

Not in this slice. A future approved apply package, produced only from human decisions recorded outside this phase, would be consumed by `vs-archive` commands for preflight, backup, apply, and independent verify. `identity-review-package-v2` does not record those decisions. A deterministic match stays pending in that package.
