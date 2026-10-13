# VS Archive people agent

Read-only pipeline for newly added or changed archive documents:

`discover -> freeze evidence -> resolve against an identity snapshot -> identity review package`

This repository does not import the VS Archive Django application, does not connect to a database, and does not call AWS. Production writes stay in `vs-archive` behind `preflight -> backup -> apply -> independent verify`.

Biography, tagging, and research are not part of this slice.

## Layout

- `src/vs_archive_people/` — discovery, evidence, resolution, identity review, artifact IO, checkpoints
- `src/vs_archive_people/schemas/` — JSON Schema documents shipped inside the package
- `tests/fixtures/` — small committed fixtures
- `docs/contracts.md` — what is consumed and what is produced

Runtime snapshots and run output belong under ignored `data/`, `runs/`, and `snapshots/` directories.

## Project context

- `.cursor/rules/` — persistent project rules
- `docs/architecture.md` — current structure
- `docs/decision-log.md` — why those decisions exist
- `docs/contracts.md` — serialized artifact contracts

## Development

Never commit directly to `main`. Start each task on its own branch. Validate and review on that branch before merge.

## Commands

Use the installed environment from this directory. `PYTHONPATH` is not required. With the virtual environment activated, `python -m vs_archive_people.cli` is the module entry point. `vs-archive-people` is the console script. Without activating the environment, call `.venv/bin/python -m vs_archive_people.cli` or `.venv/bin/vs-archive-people`.

```bash
python -m vs_archive_people.cli discover \
  --documents data/document_text_snapshot.json \
  --run-id run-001 \
  --prior-checkpoint runs/previous-checkpoint.json \
  --output runs/discovery.json

python -m vs_archive_people.cli freeze-evidence \
  --documents data/document_text_snapshot.json \
  --discovery runs/discovery.json \
  --mentions data/mention_proposals.json \
  --run-id run-001 \
  --output runs/evidence.json

python -m vs_archive_people.cli resolve \
  --evidence runs/evidence.json \
  --identity data/identity_snapshot.json \
  --output runs/resolution.json

python -m vs_archive_people.cli review-package \
  --resolution runs/resolution.json \
  --output runs/identity-review.json

python -m vs_archive_people.cli checkpoint \
  --run-id run-001 \
  --step discover \
  --input document_text_snapshot=data/document_text_snapshot.json \
  --record discovery_selection=runs/discovery.json \
  --output runs/checkpoint.discover.json
```

`--prior-checkpoint` is optional. A later checkpoint of the same run is a new file. It takes `--prior` and continues only in the order `discover -> freeze-evidence -> resolve -> review`. Every input hash already recorded for the run must be supplied again and must match. Existing output paths are refused.

`freeze-evidence` does not find person names. It validates the loose mention-proposal list and publishes `evidence-package-v1`. It does not accept `mention-proposals-v1`. `mention_key` is a run-local label such as `M0001`. `mention_fingerprint` is the durable SHA-256 of the source position. See `docs/contracts.md`.

`freeze-evidence-v2` publishes `evidence-package-v2` from `mention-proposals-v1`. It is not a checkpoint step. It re-checks every span against the displayed body. It does not read the diagnostics sidecar, and it does not assign identity. v1 `resolve` and review do not consume that package. `RUN_STEPS` remains `discover -> freeze-evidence -> resolve -> review`.

`resolve-v2` publishes `resolution-report-v2` from `evidence-package-v2` and an identity snapshot. It is not a checkpoint step. The lookup string is the frozen `name_surface`. The full `exact_mention` stays on the row for review. The command does not create or update a Person, alias, merge, or canonical name. `production_write_performed` is false. It does not read v1 evidence, and v1 `resolve` does not read v2 evidence. Review still reads only `resolution-report-v1`.

```bash
python -m vs_archive_people.cli resolve-v2 \
  --evidence runs/evidence-v2.json \
  --identity data/identity_snapshot.json \
  --run-id run-001 \
  --output runs/resolution-v2.json
```

```bash
python -m vs_archive_people.cli freeze-evidence-v2 \
  --documents data/document_text_snapshot.json \
  --discovery runs/discovery.json \
  --mentions runs/mention-proposals.json \
  --run-id run-001 \
  --output runs/evidence-v2.json
```

`package-mention-proposals` publishes `mention-proposals-v1` from human geometry. It is not a checkpoint step and it does not call a provider. v1 `freeze-evidence` does not read that file. `freeze-evidence-v2` does.

`find-mentions` publishes the same `mention-proposals-v1` artifact for a whole discovery selection. It is not a checkpoint step. A semantic detector proposes source surfaces, and a deterministic detector adds exact accepted names. The command assigns no production identity. Rejected, suppressed, and conflicting candidates go to `mention-proposal-diagnostics-v1`. Neither freeze command reads that sidecar. If the proposal file is published and the sidecar write fails, the proposal stays and the command exits nonzero.

```bash
python -m vs_archive_people.cli find-mentions \
  --documents data/document_text_snapshot.json \
  --discovery runs/discovery.json \
  --identity data/identity_snapshot.json \
  --run-id run-001 \
  --output runs/mention-proposals.json \
  --diagnostics runs/mention-proposal-diagnostics.json
```

```bash
python -m vs_archive_people.cli package-mention-proposals \
  --documents data/document_text_snapshot.json \
  --discovery runs/discovery.json \
  --geometry data/mention_geometry.json \
  --run-id run-001 \
  --output runs/mention-proposals.json
```

`--geometry` is plain JSON. Each mention supplies offsets into a displayed body, and may supply `surface_kind`. The command copies `exact_mention`, `name_surface`, `title_surface`, and `evidence_snippet` from that body.

`pilot-find-mentions` is an experimental Gemini surface detector for one displayed body. It is not automated mention finding, not a checkpoint step, and not a versioned artifact. See `docs/gemini-mention-pilot.md`.

Mention proposals for `freeze-evidence` are a JSON list, or an object with a `mentions` array. Each proposal needs `archive_item_id`, `evidence_source`, `start_offset`, `end_offset`, `exact_mention`, and `evidence_snippet`. Offsets are zero-based indexes into that displayed body, and the slice must equal `exact_mention`. Optional fields: `mention_class` (`PERSON` or `NOT_A_PERSON`), `propose_alias`, `proposed_identity_action` (`MERGE` or `CANONICAL_NAME_CHANGE`), and `confidence` (0–100). `mention_key`, `mention_fingerprint`, and `occurrence_index` are assigned when the evidence is frozen. A caller-supplied key, fingerprint, or occurrence index is discarded.

## Environment

Use this repository's own virtual environment. Do not reuse a `vs-archive` environment.

```bash
cd /home/yshif/vs-archive/people-agent
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
```

Runtime dependency: `jsonschema`. Development dependency: `ruff`. Both are declared in `pyproject.toml`. `.venv/` is ignored.

## Validation

From this directory, with the virtual environment activated:

```bash
python -m unittest discover -s tests -v
python -m ruff check src tests
python -m ruff format --check src tests
```

The same commands without activating the environment:

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m ruff check src tests
.venv/bin/python -m ruff format --check src tests
```
