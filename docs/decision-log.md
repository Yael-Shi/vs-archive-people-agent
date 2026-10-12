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

A future finder emits proposals. This slice does not implement automated finding. ADR-034 adds the generated `mention-proposals-v1` artifact and the human packager. `freeze-evidence` still consumes the loose proposal list.

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

`package-mention-proposals` is outside `RUN_STEPS` and does not call a provider. Published mention rows are ordered by archive item, evidence source, and outer offsets before the logical hash is stamped. Artifact validation rejects a duplicate outer span, more than one line break in an outer span, and a `surface_kind` that disagrees with the title slice. `freeze-evidence` does not accept `mention-proposals-v1`. Automated finding remains unimplemented. The pilot command and `GEMINI_MENTION_PILOT_NOT_ARTIFACT` are unchanged.
