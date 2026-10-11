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

A future finder emits proposals. This slice does not implement it.

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
