# Decision log

Accepted decisions for this repository. Status dates are 2026-10-11.

## ADR-001 — Standalone repository

- Date: 2026-10-11
- Status: Accepted

### Decision

Build the people pipeline in its own repository, not inside the Django application.

### Rationale

The pipeline is read-only and artifact-driven. Keeping it out of the web application avoids mixing request handling with identity review.

### Consequences

`people-agent` does not import Django or main-repository modules.

## ADR-002 — Versioned JSON artifact boundary

- Date: 2026-10-11
- Status: Accepted

### Decision

`vs-archive` and `people-agent` integrate only through explicit versioned JSON artifacts.

### Rationale

A versioned file is a reviewable contract. A shared database call is not.

### Consequences

Schemas and `docs/contracts.md` own serialization. A breaking change needs a new schema version.

## ADR-003 — No direct production DB or AWS access

- Date: 2026-10-11
- Status: Accepted

### Decision

This package does not connect to the production database or AWS.

### Rationale

Production state and writes belong to `vs-archive`.

### Consequences

Local database contents are not a source of production truth.

## ADR-004 — Do not copy the historical pipeline

- Date: 2026-10-11
- Status: Accepted

### Decision

The legacy people-agent workspace is reference only. Do not port that pipeline wholesale.

### Rationale

The new slice has a narrower contract: discovery, frozen evidence, snapshot resolution, and identity review.

### Consequences

Historical batch behavior is not an implicit requirement.

## ADR-005 — First slice ends at identity review

- Date: 2026-10-11
- Status: Accepted

### Decision

The implemented flow stops at an identity-review package.

### Rationale

Apply, biography, and research are separate decisions.

### Consequences

Commands are `discover`, `freeze-evidence`, `resolve`, `review-package`, and `checkpoint`.

## ADR-006 — Mention finding is not freeze-evidence

- Date: 2026-10-11
- Status: Accepted

### Decision

`freeze-evidence` validates and publishes proposals. It does not find names.

### Rationale

Finding names and freezing evidence are different responsibilities.

### Consequences

A future finder emits proposals. ADR-034 adds the generated `mention-proposals-v1` artifact and the human packager. ADR-035 adds automated `find-mentions` and the diagnostics sidecar. `freeze-evidence` still consumes the loose proposal list.

## ADR-007 — Title is metadata

- Date: 2026-10-11
- Status: Accepted

### Decision

Item title is not evidence text.

### Rationale

Evidence must be declared displayed body text.

### Consequences

Discovery does not select an item because only a title changed.

## ADR-008 — Displayed OCR and manual text are the evidence source

- Date: 2026-10-11
- Status: Accepted

### Decision

Evidence comes from displayed OCR transcription, displayed source text, displayed Hebrew translation, and manual-text body.

### Rationale

Those are the texts a reviewer can see. Identical raw bodies may share roles. Whitespace-only differences stay distinct.

### Consequences

Displayed-text hashes drive change detection.

## ADR-009 — Exact canonical or alias match only

- Date: 2026-10-11
- Status: Accepted

### Decision

A deterministic match is an exact normalized production `Person.name` or existing `PersonAlias.name`, and exactly one such person.

### Rationale

Identity classification must be reproducible from the snapshot.

### Consequences

Shared names stay ambiguous. Unknown names stay new-person candidates.

## ADR-010 — No fuzzy matching or title stripping

- Date: 2026-10-11
- Status: Accepted

### Decision

Normalization is NFKC, casefold, and whitespace collapse. Names are not fuzzy-matched and titles are not stripped.

### Rationale

Extra normalization would silently merge distinct names.

### Consequences

Near-matches are not auto-classified as the same person.

## ADR-011 — Confidence does not approve identity

- Date: 2026-10-11
- Status: Accepted

### Decision

Confidence may be stored. It does not choose a class or approve a row.

### Rationale

A numeric score is not a human decision.

### Consequences

Classification ignores confidence.

## ADR-012 — Pending and local candidates are not production identity

- Date: 2026-10-11
- Status: Accepted

### Decision

`PENDING` snapshot rows and local candidates are excluded from the production index.

### Rationale

Only accepted production names are match targets.

### Consequences

Those names cannot produce a deterministic production match.

## ADR-013 — Source offsets are durable mention identity

- Date: 2026-10-11
- Status: Accepted

### Decision

`mention_fingerprint` is the SHA-256 of archive item, evidence source, source-text hash, and start and end offsets. The raw body slice must equal `exact_mention`.

### Rationale

Occurrence index and proposal spelling shift when an earlier mention appears or harmless whitespace changes.

### Consequences

Inserting an earlier span does not change an existing fingerprint. A wrong slice fails closed.

## ADR-014 — mention_key is run-local

- Date: 2026-10-11
- Status: Accepted

### Decision

`mention_key` is assigned per run from source order. It is not durable identity.

### Rationale

Run-local labels stay short and stable inside one package. They must not be reused as cross-run identity.

### Consequences

Caller keys are discarded. Resolution and review copy the generated key for that package.

## ADR-015 — Logical hash and file hash are distinct

- Date: 2026-10-11
- Status: Accepted

### Decision

Logical content hash excludes publication metadata and audit-only `*_file_sha256` fields. Physical file hash covers the published bytes. A child binds a generated parent by logical hash and may record the parent file hash for audit.

### Rationale

A rebuilt parent with the same logical content and a new timestamp must not change the child's logical identity.

### Consequences

Checkpoint file-hash maps use names that stay inside the checkpoint logical body, because a checkpoint is the exact-file record of a step.

## ADR-016 — Verify logical hashes on consumption

- Date: 2026-10-11
- Status: Accepted

### Decision

A generated artifact is schema-validated and its logical hash is recomputed when a later step consumes it. A mismatch is `LOGICAL_HASH_MISMATCH`.

### Rationale

A stored hash that is only well-formed is not an integrity check.

### Consequences

External document-text and identity snapshots are not checked this way. They have no generated logical provenance.

## ADR-017 — Draft 2020-12 via jsonschema

- Date: 2026-10-11
- Status: Accepted

### Decision

Artifact JSON Schema is Draft 2020-12, validated with `jsonschema`.

### Rationale

A custom schema interpreter drifted from the standard.

### Consequences

`jsonschema==4.23.0` is the runtime dependency. No second interpreter.

## ADR-018 — Schemas are package resources

- Date: 2026-10-11
- Status: Accepted

### Decision

Schemas live in the package and load through `importlib.resources`. `SCHEMA_FILES` maps each artifact `schema_version` to one filename.

### Rationale

Validation must work from an installed environment, outside the repository working directory. Filename mapping must not depend on scanning for a matching const.

### Consequences

A duplicate `$id`, a missing mapped file, or a mismatched `schema_version` fails closed. `producer.v1.json` is a shared schema and has no `schema_version`.

## ADR-019 — Strict checkpoint progression

- Date: 2026-10-11
- Status: Accepted

### Decision

A run moves only `discover -> freeze-evidence -> resolve -> review`. Each later checkpoint is a new file. Previously bound input file hashes must be presented again unchanged.

### Rationale

Skipped or rewritten history would hide which step produced which file.

### Consequences

An unsupported checkpoint schema version fails as `UNSUPPORTED_SCHEMA_VERSION` and is not migrated. ADR-029 refines which checkpoints may seed a later discovery run.

## ADR-020 — Python owns progression order

- Date: 2026-10-11
- Status: Accepted

### Decision

`RUN_STEPS` is the state machine. The schema enum only validates the allowed step strings.

### Rationale

JSON Schema `enum` is a set. Its array order must not own business behavior.

### Consequences

CLI choices and checkpoint progression both read `RUN_STEPS`.

## ADR-021 — Immutable atomic no-clobber publication

- Date: 2026-10-11
- Status: Accepted

### Decision

Publication writes a complete temporary file in the destination directory, fsyncs it, then `os.link`s it to the destination. An existing path raises `ArtifactExistsError`. The parent directory is fsynced after the link.

### Rationale

`os.replace` can still overwrite a file created between an `exists` check and the rename. `link` fails if the name exists. Writing the temp file first avoids publishing a partial destination.

### Consequences

The destination directory must support hard links, which holds for this repository on Linux/WSL. The no-overwrite guarantee does not depend on `Path.exists`.

## ADR-022 — Review is not apply

- Date: 2026-10-11
- Status: Accepted

### Decision

Human review is mandatory for new people, ambiguity, merges, canonical-name changes, inferred aliases, biography, and production writes. A deterministic match is not an apply operation.

### Rationale

Identity review and production mutation are different authorities.

### Consequences

The review package kind is identity review, not apply. Biography is not included.

## ADR-023 — Dedicated virtual environment

- Date: 2026-10-11
- Status: Accepted

### Decision

This repository uses its own `.venv`. Do not reuse a `vs-archive` environment.

### Rationale

Dependencies and Python version are independent of the Django application.

### Consequences

`.venv/` is ignored. Normal commands use the installed package, not `PYTHONPATH=src`.

## ADR-024 — Python 3.12 minimum

- Date: 2026-10-11
- Status: Accepted

### Decision

`requires-python` is `>=3.12`. Ruff `target-version` is `py312`.

### Rationale

The package should target one current minimum rather than keep a 3.11 floor.

### Consequences

Development and CI for this repository use Python 3.12 or newer.

## ADR-025 — Ruff is the lint and format tool

- Date: 2026-10-11
- Status: Accepted

### Decision

Ruff `0.14.14` is the linter and formatter. Pyright and mypy are not used.

### Rationale

Match the main repository's Ruff pin without adopting its Django type-checker stack.

### Consequences

`ruff check` and `ruff format --check` are the static quality gates.

## ADR-026 — Controlled CLI read and parse errors

- Date: 2026-10-11
- Status: Accepted

### Decision

Invalid JSON is `MALFORMED_ARTIFACT`. A missing, unreadable, or non-UTF-8 input is `ARTIFACT_READ_ERROR`. The CLI catches `ArtifactError` and prints `BLOCKED` JSON.

### Rationale

A bad input file must not surface as an uncaught traceback. Filesystem failure is not the same class of problem as malformed JSON.

### Consequences

Programmer errors are not caught by that handler. No output artifact is created on these failures.

## ADR-027 — Do not commit generated packaging metadata

- Date: 2026-10-11
- Status: Accepted

### Decision

Ignore `*.egg-info/`, `build/`, and `dist/`. Do not commit them.

### Rationale

Editable installs write `src/*.egg-info`. That metadata is generated and goes stale.

### Consequences

The generated `src/vs_archive_people.egg-info/` directory is not part of the source tree.

## ADR-028 — Never commit directly to main

- Date: 2026-10-11
- Status: Accepted

### Decision

Every task starts on a dedicated branch. `main` receives changes only after validation and review on that branch.

### Rationale

The first commit and every later change need a reviewable branch boundary.

### Consequences

Agents do not commit, push, or merge unless the user explicitly asks. `git add .` is not the staging method.

## ADR-029 — Only a complete checkpoint seeds discovery

- Date: 2026-10-11
- Status: Accepted

### Decision

Partial checkpoints stay resumable for the same run through `append_step`. Discovery `--prior-checkpoint` requires `completed_steps` to equal `RUN_STEPS` in that order. Anything shorter is `INCOMPLETE_PRIOR_CHECKPOINT`.

### Rationale

ADR-019 allows `processed_items` to be stored at `freeze-evidence`. Using that partial file as the next run's memory would hide items whose later review never finished. A completed checkpoint may still belong to a different `run_id`.

### Consequences

Same-run resume is unchanged. Cross-run discovery memory starts only after review.

## ADR-030 — Audit-bind output-affecting external files

- Date: 2026-10-11
- Status: Accepted

### Decision

The evidence package stores required `mention_proposals_file_sha256`. The resolution report stores `local_candidates_file_sha256`, null when `--local-candidates` is omitted. Both are physical file hashes and stay outside the logical content hash.

### Rationale

Those files change what the command publishes, but they are not generated artifacts and have no logical provenance of their own. The frozen mentions and resolution rows remain the semantic content.

### Consequences

Reformatting a proposal file without changing the frozen mentions changes only the audit hash. The resolution builder requires the local-candidate list and its file hash to be absent together or present together. An empty list still requires the hash. `local_candidates_are_production_identity` stays false.

## ADR-031 — Gemini mention detection stays an isolated pilot

- Date: 2026-10-11
- Status: Accepted

### Decision

Person-name surface detection with Gemini 3.8 Flash is an experimental pilot (`pilot-find-mentions`). It is outside `RUN_STEPS`. It does not publish `mention-proposals-v1` or any other versioned artifact. Gemini may propose a surface inside a segment. Local code computes and checks source offsets. Gemini does not assign a production `person_id`, choose a canonical identity, create an alias, suggest a merge, or approve a Person.

### Rationale

Recall of name-like spans in real archive text is still unmeasured. Wiring that experiment into discovery, evidence, or resolution would treat an unreviewed model output as pipeline evidence.

### Consequences

The pilot reads a document-text snapshot and an identity snapshot, calls Gemini only for surface proposals, and writes `kind: GEMINI_MENTION_PILOT_NOT_ARTIFACT`. A known-name pass is local and exact. It also does not assign identity. See `docs/gemini-mention-pilot.md`.

## ADR-032 — Mention-pilot surfaces stay exact and non-official

- Date: 2026-10-12
- Status: Accepted

### Decision

The Gemini mention pilot remains outside `RUN_STEPS` and still does not publish `mention-proposals-v1`. After the first 10-item corpus run, local checks reduce specific false positives without treating the pilot as evidence.

A title or role with no name is not a person candidate. A title plus a name is one mention. The result may store the exact full slice, the exact name slice, and the exact title slice. Offsets refer to the original body. A split that is not an exact unique substring is not invented; the full slice remains the name.

A span that only continues `רחוב`, and a span marked `NON_PERSON`, is not a person candidate. The same string can still be a candidate in another occurrence. Known-name search does not override that. Hebrew core names are stored only when the proposed remainder is an exact substring behind a safe attached proclitic. Two adjacent segments may form one span when the gap is a single newline, and that newline stays in the slice.

### Rationale

The corpus run kept real partial, OCR, and multilingual names, and it also returned title-only roles, street occurrences of person strings, prefixed forms, and names split by a line break. Promoting those outputs into the official pipeline would freeze the false positives. Dropping every difficult span would lose the recall the pilot is measuring.

### Consequences

`pilot-find-mentions` can be rerun on the same document-text snapshot, the same identity snapshot, and the same evidence source. The prompt and the local filters changed, so the JSON result can change. The pilot kind stays `GEMINI_MENTION_PILOT_NOT_ARTIFACT`. See `docs/gemini-mention-pilot.md`.

ADR-033 supersedes one sentence in the decision above: an invalid title split does not leave the full slice as the name. A missing or invalid `TITLE_AND_NAME` split is `TITLE_SPLIT_UNRESOLVED` and is not published as a name. The rest of this decision stands.

## ADR-033 — Title splits are exact Gemini slices, not a vocabulary

- Date: 2026-10-12
- Status: Accepted

### Decision

VS Archive stores a title as free text on `Person.honorific`. There is no title vocabulary and no parser. The mention pilot does not add one. For `TITLE_AND_NAME`, Gemini must supply the exact full surface, the exact title slice, and the exact name slice. Local code checks those slices against the original body. A missing or invalid split is `TITLE_SPLIT_UNRESOLVED` and is not published as a name.

### Rationale

A copied honorific-prefix list would reject real archive titles such as `אלוף`, `שר הבטחון`, and `השגריר האמריקאי`, and it would pretend the main app has a title catalog. The catalog does not exist.

### Consequences

The pilot still assigns no `person_id`. Attached forms such as `והאלוף` stay exact. See `docs/gemini-mention-pilot.md`.

## ADR-034 — mention-proposals-v1 is geometry, packaged by a human command

- Date: 2026-10-12
- Status: Accepted

### Decision

`mention-proposals-v1` is a generated artifact for person-name geometry. `package-mention-proposals` is its human producer. The command reads a document-text snapshot, a discovery selection, and a plain geometry file of offsets plus an optional `surface_kind`. It derives `exact_mention`, `name_surface`, `title_surface`, and `evidence_snippet` from the unmodified displayed body, stamps provenance and the logical hash, and publishes with the existing atomic no-clobber writer.

The human file records one detector, `{id: "human-authored", role: "human", implementation: "manual", version: "1"}`, sets every mention `detector_ids` to that id, and sets `known_name_match_quality` to null. `identity_snapshot_file_sha256` is null. `production_write_performed` is false.

The artifact has no diagnostics field and no identity fields: no `person_id`, `mention_key`, `mention_fingerprint`, `occurrence_index`, `mention_class`, `propose_alias`, `proposed_identity_action`, or `confidence`. Titles are free text. There is no title vocabulary. Detector `role` is a non-empty string, not a closed schema enum.

`RUN_STEPS` stays `discover -> freeze-evidence -> resolve -> review`. v1 freeze, resolution, review, and `run-checkpoint-v1` stay on the loose proposal list. This phase does not implement automated `find-mentions`, the diagnostics sidecar, or v2 evidence, resolution, review, or checkpoint artifacts.

### Rationale

Detection and evidence freezing are different responsibilities (ADR-006). A human still needs a stamped proposal file whose slices are copied from source text rather than typed by hand. Publishing that file does not require a model call, and it must not change the in-flight v1 pipeline.

### Consequences

`package-mention-proposals` is outside `RUN_STEPS` and does not call a provider. Published mention rows are ordered by archive item, evidence source, and outer offsets before the logical hash is stamped. Artifact validation rejects a duplicate outer span, more than one line break in an outer span, and a `surface_kind` that disagrees with the title slice. `freeze-evidence` does not accept `mention-proposals-v1`. The pilot command and `GEMINI_MENTION_PILOT_NOT_ARTIFACT` are unchanged. ADR-035 adds the automated producer and the diagnostics sidecar. v1 freeze still does not read official proposals.

## ADR-035 — Official find-mentions publishes proposals and a diagnostic sidecar

- Date: 2026-10-13
- Status: Accepted

### Decision

`find-mentions` is the official automated producer of `mention-proposals-v1`. It stays outside `RUN_STEPS`. One successful run publishes one proposal for the whole discovery selection. The semantic detector proposes source surfaces behind a provider-neutral interface. The first implementation is Gemini 3.8 Flash, with structured output, `ThinkingLevel.LOW`, default decoding, SDK retries of three attempts, a 120 second timeout, and no tools. The known-name detector searches accepted production names as exact literals and publishes `EXACT_BOUNDARY` and a safe `PREFIXED` peel. `EMBEDDED` is diagnostic only. Mention rows reference `detector_ids`. They carry no provider payload and no `person_id`.

`mention-proposal-diagnostics-v1` is a separate generated artifact. It binds the proposal logical hash and the proposal file hash, and it records rejected, suppressed, and conflict rows. Freeze does not read it. There is no two-file transaction. A failure before proposal publication publishes nothing. If the proposal link succeeds and the sidecar link fails, the proposal stays and the CLI reports `PROPOSAL_PUBLISHED` with `DIAGNOSTICS_PUBLICATION_FAILED`.

Same-span detector hits merge. Conflicting exact splits publish neither row. Exactly one titled outer absorbs an inner name span. Several titled outers keep every valid span. A provider failure on any required body publishes no partial selection. `pilot-find-mentions` stays available. v1 freeze, resolution, review, and `run-checkpoint-v1` are unchanged.

### Rationale

The pilot showed that hybrid surface detection is usable, and the proposal contract from ADR-034 is the official shape of that result. Detection is still not evidence. Putting it in `RUN_STEPS`, or folding diagnostics into the proposal logical body, would make review-complete discovery memory depend on a model call and on rows freeze does not use.

### Consequences

The command reads a document-text snapshot, a discovery selection, and an identity snapshot. It writes no production data and creates no alias or merge. Official proposals are not a v1 freeze input. Identity-review-package-v2 and run-checkpoint-v2 remain unimplemented. ADR-036 adds evidence-package-v2. ADR-037 adds resolution-report-v2. See `docs/contracts.md`.

## ADR-036 — evidence-package-v2 is the freeze trust boundary

- Date: 2026-10-13
- Status: Accepted

### Decision

`freeze-evidence-v2` publishes `evidence-package-v2` from a document-text snapshot, a discovery selection, and `mention-proposals-v1`. v1 `freeze-evidence` stays on the loose proposal list and rejects `mention-proposals-v1`. `mention-proposals-v1` is detection geometry. Freeze v2 is the trust boundary: it verifies the proposal artifact and then re-slices every outer, name, and title span from the unmodified displayed body. The diagnostics sidecar is not an input. Freeze does not open the identity snapshot named on the proposal and does not copy that hash as an evidence parent. Freeze assigns no `person_id` and no alias, merge, or canonical-name action.

Occurrence identity stays the v1 fingerprint: archive item, evidence source, displayed-body hash, and outer offsets. Detector provenance does not create a new occurrence. `RUN_STEPS` stays `discover -> freeze-evidence -> resolve -> review`. This phase does not implement resolution-report-v2, identity-review-package-v2, run-checkpoint-v2, checkpoint migration, or retirement of v1 freeze.

### Rationale

ADR-006 separates finding names from freezing evidence. ADR-034 and ADR-035 publish official geometry outside the v1 pipeline so detection can be reviewed without becoming evidence. Feeding that file to v1 freeze would mix loose proposal identity fields into a contract that no longer carries them. A second freeze command keeps v1 callers unambiguous and makes the source re-check the point where geometry becomes evidence.

### Consequences

`evidence-package-v2` binds discovery and the proposal by logical hash, and records the document, discovery, and proposal file hashes for audit under the existing `*_file_sha256` exclusion rule. A wrong generation fails closed. v1 resolution, review, and checkpoints do not read the v2 package. ADR-037 adds `resolve-v2`. Review v2 and checkpoint v2 remain unimplemented. See `docs/contracts.md`.

## ADR-037 — resolution-report-v2 resolves verified name_surface without writing production

- Date: 2026-10-13
- Status: Accepted

### Decision

`resolve-v2` publishes `resolution-report-v2` from a verified `evidence-package-v2` and an identity snapshot. It is outside `RUN_STEPS`. v1 `resolve` still reads only `evidence-package-v1` and still matches `exact_mention`. There is no automatic migration between the two commands.

Evidence v2 is already the source trust boundary. Resolve v2 does not re-slice displayed text, strip a title, or peel a Hebrew proclitic. The identity lookup string is the frozen `name_surface`. `exact_mention`, title offsets, and prefix geometry stay on the resolution row as evidence and display context.

Classifications are `DETERMINISTIC_EXISTING_PERSON`, `AMBIGUOUS`, and `NEW_PERSON_CANDIDATE`. `NOT_A_PERSON` is not a v2 state. Non-person candidates were suppressed before evidence publication. `detector_ids`, `surface_kind`, and `known_name_match_quality` are copied for review. They do not force a production match, override ambiguity, create a person, or approve identity. A known-name detector found a literal surface. It did not assign `person_id`.

The report binds the evidence package by logical hash and records the evidence file hash and the identity snapshot file hash for audit. Those file hashes use the existing `*_file_sha256` exclusion rule. The identity snapshot is not a generated logical parent. Local candidates are not a v2 input. In v1 they only set `local_candidate_overlap` for review; they never become production identity, and identity-review-package-v2 is not implemented. `production_write_performed` is false.

`RUN_STEPS` stays `discover -> freeze-evidence -> resolve -> review`. This phase does not implement identity-review-package-v2, review v2, run-checkpoint-v2, checkpoint migration, v1 retirement, or any apply or write workflow.

### Rationale

ADR-036 freezes a name slice that is already checked against the displayed body. Matching the outer mention again would treat a title or a proclitic as part of the person's name, which the freeze step has already separated. A second command keeps v1 callers on `exact_mention` and makes the lookup field explicit. Human review is still required before any future production write.

### Consequences

`resolve-v2` fails closed on `evidence-package-v1`, a logical-hash mismatch, or a different `run_id`. v1 `resolve` fails closed on `evidence-package-v2`. Rows are ordered by `mention_key`. One production person is deterministic, several are ambiguous, and none is a new-person candidate. No Person, alias, merge, or canonical name is created or changed. See `docs/contracts.md`.

## ADR-038 — identity-review-package-v2 keeps every resolution pending for human review

- Date: 2026-10-13
- Status: Accepted

### Decision

`review-v2` publishes `identity-review-package-v2` from a verified `resolution-report-v2`. It is outside `RUN_STEPS`. v1 `review-package` still reads only `resolution-report-v1`. A `resolution-report-v2` input to that command is `UNSUPPORTED_SCHEMA_VERSION`. There is no automatic migration.

Every v2 resolution becomes one `review_rows` entry with `review_status` `PENDING`. That includes `DETERMINISTIC_EXISTING_PERSON`. The review reason for that class is `deterministic_existing_person_pending_review`. There is no v2 `auto_resolution_proposals` list and no `proposal_status`. `identity_is_production` records that the resolution named an existing production person. It does not record human approval.

`production_write_performed`, `human_approval_performed`, and `apply_authorized` are false. The package does not create a Person, alias, merge, or canonical-name change, and it does not store a human decision. Detector provenance is copied for the reviewer and does not approve a row. `run-checkpoint-v2` stays unimplemented. The v1 checkpoint schema map still records `identity-review-package-v1`.

### Rationale

ADR-037 can name exactly one production person and still must not treat that match as permission to write. The v1 auto-resolution list is an audit proposal for a different contract. Reusing it in v2 would let a deterministic row leave the human queue before a person has approved it.

### Consequences

`review-v2` fails closed on `resolution-report-v1`, a logical-hash mismatch, a different `run_id`, a duplicate mention identity, or a resolution row whose match fields disagree with its classification. v1 review of a valid `resolution-report-v1` is unchanged, including `auto_resolution_proposals`. `RUN_STEPS` stays `discover -> freeze-evidence -> resolve -> review`. See `docs/contracts.md`.

## ADR-039 — run-checkpoint-v2 preserves one generation per run

- Date: 2026-10-13
- Status: Accepted

### Decision

`checkpoint-v2` publishes `run-checkpoint-v2`. `RUN_STEPS` stays `discover -> freeze-evidence -> resolve -> review` for v1 and v2. Those strings are the only checkpoint step names. `checkpoint` still publishes and resumes `run-checkpoint-v1`.

A run has one generation. The schema-version map recorded at `discover` is copied forward. A v1 checkpoint does not accept `evidence-package-v2`, `resolution-report-v2`, or `identity-review-package-v2`. A v2 checkpoint does not accept the v1 counterparts. Partial checkpoints resume only inside the generation they started with. There is no migration.

A completed checkpoint of either generation may seed discovery for a new run when `completed_steps` equals `RUN_STEPS`. The new run has its own `run_id`. Reading a completed v1 checkpoint as that memory does not resume the old run as v2.

Completing `review` means the identity-review package was published. For v2 that package remains a pending queue. The checkpoint does not record human approval, apply authorization, or a production write. Mention proposals stay outside both schema maps.

### Rationale

The v2 artifacts are a second contract, not a replacement of an in-flight v1 run. Encoding generation in the step names would make `RUN_STEPS` describe products instead of the workflow. Encoding it only in the checkpoint schema keeps resume, discovery memory, and the v1 command unambiguous.

### Consequences

`published_schema_versions()` remains the v1 map. The v2 map is requested explicitly. Same-run resume of the other generation is `UNSUPPORTED_SCHEMA_VERSION`. An incomplete prior checkpoint of either generation is `INCOMPLETE_PRIOR_CHECKPOINT`. See `docs/contracts.md`.

## ADR-041 — same-person alias duplicates are one lookup

- Date: 2026-10-14
- Status: Accepted

### Decision

`build_index` treats multiple `PersonAlias` rows that normalize to one key and point at one Person as one identity candidate. `matched_alias_id` is the lowest alias id in that group. Both raw alias strings remain exact known-name search strings. The snapshot rows are not deleted or rewritten.

The same normalized key on two different production people stays ambiguous. `matched_alias_id` is null unless the match is one person reached through `person_alias`.

`project_to_identity_snapshot_v1` copies a biography identity export by setting `schema_version` to `identity-snapshot-v1`. It does not remove names.

### Rationale

Normalization already collapses whitespace, including a line break. Two alias records for one person are two spellings of one lookup surface, not two people. Failing the index on that pair blocks a faithful snapshot without changing who the name refers to. Choosing the lowest alias id is stable if the export row order changes. A shared name across people is still not one person.

### Consequences

A biography export can be projected into `identity-snapshot-v1` without editing alias text. Known-name detection still searches every original alias string. See `docs/contracts.md`.

## ADR-042 — comparison is by source role

- Date: 2026-10-14
- Status: Accepted

### Decision

`compare-mention-runs` writes `mention-run-comparison-v2`. A mention is comparable only when both runs evaluated the same `archive_item_id` and `evidence_source`. A role present on only one side is `OFFICIAL_SOURCE_NONCOMPARABLE` or `PILOT_SOURCE_NONCOMPARABLE`. Those rows are omitted from official-only, pilot-only, overlap, and disagreement counts.

Aggregate `source_equivalence` summarizes comparable pairs only. `source_equivalence_summary` records whether those pairs are all equivalent and how many non-comparable roles remain. Per-source status stays on the pair.

Exact duplicate diagnostic records stay in the raw diagnostic list. Unique occurrence counts use archive item, evidence source, section, reason, offsets, surface, and segment id. Markdown table cells escape pipes and line breaks without changing the JSON strings.

### Rationale

The historical pilot for item 292 read `ocr_document.displayed_transcription.text`. The official run also published `ocr_document.displayed_hebrew_translation.text`. Counting those Hebrew mentions as `OFFICIAL_ONLY` treats an unevaluated source as a detector disagreement. Duplicate diagnostic rows were already in the sidecar. A review table must stay one row per logical result when the source text contains a line break or a pipe.

### Consequences

The first published comparison file remains `mention-run-comparison-v1` and is not overwritten. Later comparisons use v2. This does not change mention finding. See `docs/contracts.md`.

## ADR-043 — official evaluation needs positive evidence

- Date: 2026-10-14
- Status: Accepted

### Decision

`compare-mention-runs` writes `mention-run-comparison-v3`. A pilot source is not officially evaluated merely because the document snapshot is missing or its file hash does not match the proposal. Official evaluation of one archive item and evidence source requires an official mention, an official diagnostic, or a proposal-bound discovery selection together with the matching snapshot role that `find-mentions` runs. A displayed body in the snapshot does not by itself prove the item was selected.

`overlap_different_span_count` counts mention rows in an overlap group. One pilot row overlapping one official row counts as 2. No new metric is added for unique adjudication cases.

### Rationale

Treating a missing snapshot as evaluation turned a one-sided pilot source into `PILOT_ONLY`. `find-mentions` runs every displayed body of a discovery-selected item, including bodies that publish no mention and no diagnostic. The snapshot alone does not name that selection. The discovery file already stored on the proposal is the selection evidence.

### Consequences

v1 and v2 schema files stay as published. `--discovery` is optional and evaluation-only. It is not a `RUN_STEPS` entry. See `docs/contracts.md`.

## ADR-044 — mention comparison disagreements require explicit human adjudication before tuning

- Date: 2026-10-14
- Status: Accepted

### Decision

`package-mention-adjudication` writes `mention-adjudication-v1` from `mention-run-comparison-v3` only. Comparison review rows are not human cases one-to-one. Overlap rows in one connected component are one case. Diagnostics are evidence on a related occurrence, not a decision, and become a standalone case only when no mention case matches. Every case begins `PENDING` with a null decision.

Human decisions are a separate `mention-adjudication-decisions-v1` file bound to the packet logical hash. That file is not stamped. This phase does not tune the finder, change prompts, split rules, suppressions, or known-name behavior, retire the pilot or v1, change production identity, create Person or alias records, merge identities, or authorize apply.

### Rationale

The comparison already showed that one disagreement can be two directional rows, and that a diagnostic can describe the same span as a pilot-only mention. Treating each row as a verdict would double-count overlap and would treat a suppression reason as an approval. The pilot is not ground truth, and known-name support is not a decision.

### Consequences

The adjudication packet is immutable generated evidence. Later review edits the decision file. That file is valid for downstream use only when `require_mention_adjudication_decisions` accepts it: the packet hashes match, and the decision case ids are exactly the packet case ids, once each. Row order is not meaningful. A diagnostic that overlaps more than one logical case stays a standalone case and is not assigned by span length or case order. No downstream tuning reads those decisions yet. See `docs/contracts.md`.

## ADR-040 — corpus comparison precedes pilot/v1 retirement

- Date: 2026-10-13
- Status: Accepted

### Decision

Official v2 is now end-to-end: `discover -> find-mentions -> freeze-evidence-v2 -> resolve-v2 -> review-v2 -> checkpoint-v2`. The logical checkpoint steps remain `discover -> freeze-evidence -> resolve -> review`.

`compare-mention-runs` is a read-only evaluation of already written mention output. It publishes `mention-run-comparison-v1` and a Markdown review report. It is not a `RUN_STEPS` entry and it is not in either checkpoint schema map. It does not call Gemini or any other provider.

The historical mention-pilot output is not automatically ground truth. The comparison does not report precision or recall unless a separate human-reviewed ground truth exists. Detector disagreement is a review packet, not an adjudication. Source drift is recorded separately from detector differences. No component is retired from aggregate counts. Retirement of the pilot or of v1 happens only after that comparison evidence has been reviewed.

The first pilot corpus, in selection order, is archive items 22, 156, 182, 184, 268, 273, 288, 292, 297, and 323. Item 288 uses `manual_text.body`. The other nine use `ocr_document.displayed_transcription.text`.

### Rationale

The official finder and the pilot can both be wrong. Retiring one because it produced fewer rows, or treating the pilot as the answer key, would hide source drift and unresolved spans. A frozen comparison can be repeated without another model call.

### Consequences

`mention-run-comparison-v1` uses the existing provenance stamp and logical hash, and it stays in the evaluation namespace. Wrong or hash-mismatched official artifacts fail closed. Missing diagnostics do not block the comparison; diagnostic metrics stay unavailable. See `docs/contracts.md` and `docs/gemini-mention-pilot.md`.
