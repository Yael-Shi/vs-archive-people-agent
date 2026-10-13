"""Command line for the read-only people slice.

Pipeline commands: discover, freeze-evidence, resolve, review-package, checkpoint.
``freeze-evidence`` reads a loose proposal list and publishes evidence-package-v1.
``freeze-evidence-v2`` reads mention-proposals-v1 and publishes evidence-package-v2.
It is not a pipeline step. ``package-mention-proposals`` publishes
mention-proposals-v1 from human geometry. ``find-mentions`` publishes the same
artifact from automated detection, plus a diagnostic sidecar.
``pilot-find-mentions`` is experimental. None of those commands is a pipeline
step. Each output path is published atomically and is refused when it already
exists.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from vs_archive_people.artifacts.io import (
    EVIDENCE_PACKAGE_SCHEMA,
    MENTION_PROPOSALS_SCHEMA,
    ArtifactError,
    RunIdMismatchError,
    UnsupportedSchemaVersionError,
    read_json,
    sha256_file,
    write_json_atomic,
)
from vs_archive_people.find_mentions.errors import DiagnosticsPublicationFailed
from vs_archive_people.find_mentions.gemini import GeminiSemanticDetector
from vs_archive_people.find_mentions.run import publish_mention_findings
from vs_archive_people.artifacts.provenance import (
    stamp_generated_artifact,
    verify_generated_artifact,
)
from vs_archive_people.discovery.select import select_items
from vs_archive_people.evidence.package import build_evidence_package
from vs_archive_people.evidence.package_v2 import build_evidence_package_v2
from vs_archive_people.identity_review.package import build_review_package
from vs_archive_people.resolution.match import build_resolution_report
from vs_archive_people.resolution.snapshot_index import build_index
from vs_archive_people.artifacts.schema_validate import step_chain
from vs_archive_people.mention_pilot.gemini import GeminiMentionDetector
from vs_archive_people.mention_proposals.package import package_mention_proposals
from vs_archive_people.mention_pilot.run import (
    displayed_body_for_item,
    run_mention_pilot,
)
from vs_archive_people.runs.checkpoint import (
    append_step,
    initial_checkpoint,
    processed_items_from_evidence,
)


def _proposals(document: object) -> list[dict]:
    if (
        isinstance(document, dict)
        and document.get("schema_version") == MENTION_PROPOSALS_SCHEMA
    ):
        raise UnsupportedSchemaVersionError(
            "freeze-evidence reads a loose proposal list; "
            "mention-proposals-v1 requires freeze-evidence-v2"
        )
    if isinstance(document, list):
        return document
    if isinstance(document, dict) and isinstance(document.get("mentions"), list):
        return document["mentions"]
    raise ArtifactError("mention proposals must be a list or an object with mentions")


def _local_candidates(path: Path | None) -> list[dict] | None:
    if path is None:
        return None
    document = read_json(path)
    if isinstance(document, list):
        return document
    if isinstance(document, dict) and isinstance(document.get("candidates"), list):
        return document["candidates"]
    raise ArtifactError("local candidates must be a list or an object with candidates")


def _bindings(values: list[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    for raw in values:
        if "=" not in raw:
            raise ArtifactError(f"expected name=path, got {raw!r}")
        name, path_value = raw.split("=", 1)
        binding_name = name.strip()
        if not binding_name or not path_value.strip():
            raise ArtifactError(f"expected name=path, got {raw!r}")
        if binding_name in found:
            raise ArtifactError(f"duplicate binding name {binding_name!r}")
        found[binding_name] = sha256_file(Path(path_value))
    return found


def command_discover(args: argparse.Namespace) -> int:
    snapshot = read_json(args.documents)
    prior = read_json(args.prior_checkpoint) if args.prior_checkpoint else None
    selection = select_items(snapshot, prior)
    selection["document_snapshot_file_sha256"] = sha256_file(args.documents)
    if args.prior_checkpoint:
        selection["prior_checkpoint_file_sha256"] = sha256_file(args.prior_checkpoint)
        selection["prior_checkpoint_logical_sha256"] = prior["logical_content_sha256"]
    else:
        selection["prior_checkpoint_file_sha256"] = None
        selection["prior_checkpoint_logical_sha256"] = None
    selection["run_id"] = args.run_id
    stamp_generated_artifact(selection)
    digest = write_json_atomic(args.output, selection)
    _ok({"output_sha256": digest, "selected": len(selection["selected_items"])})
    return 0


def command_freeze_evidence(args: argparse.Namespace) -> int:
    snapshot = read_json(args.documents)
    discovery = read_json(args.discovery)
    package = build_evidence_package(
        snapshot,
        discovery,
        _proposals(read_json(args.mentions)),
        run_id=args.run_id,
        document_snapshot_file_sha256=sha256_file(args.documents),
        discovery_file_sha256=sha256_file(args.discovery),
        mention_proposals_file_sha256=sha256_file(args.mentions),
    )
    digest = write_json_atomic(args.output, package)
    _ok({"output_sha256": digest, "mentions": len(package["mentions"])})
    return 0


def command_freeze_evidence_v2(args: argparse.Namespace) -> int:
    """Publish evidence-package-v2 from mention-proposals-v1."""
    snapshot = read_json(args.documents)
    discovery = read_json(args.discovery)
    package = build_evidence_package_v2(
        snapshot,
        discovery,
        read_json(args.mentions),
        run_id=args.run_id,
        document_snapshot_file_sha256=sha256_file(args.documents),
        discovery_file_sha256=sha256_file(args.discovery),
        mention_proposals_file_sha256=sha256_file(args.mentions),
    )
    digest = write_json_atomic(args.output, package)
    _ok(
        {
            "output_sha256": digest,
            "logical_content_sha256": package["logical_content_sha256"],
            "schema_version": package["schema_version"],
            "mentions": len(package["mentions"]),
        }
    )
    return 0


def command_resolve(args: argparse.Namespace) -> int:
    evidence = read_json(args.evidence)
    identity = read_json(args.identity)
    local_path = args.local_candidates
    report = build_resolution_report(
        evidence,
        build_index(identity),
        evidence_package_file_sha256=sha256_file(args.evidence),
        identity_snapshot_file_sha256=sha256_file(args.identity),
        local_candidates=_local_candidates(local_path),
        local_candidates_file_sha256=sha256_file(local_path) if local_path else None,
    )
    digest = write_json_atomic(args.output, report)
    _ok({"output_sha256": digest, "resolutions": len(report["resolutions"])})
    return 0


def command_review(args: argparse.Namespace) -> int:
    report = read_json(args.resolution)
    package = build_review_package(
        report,
        resolution_report_file_sha256=sha256_file(args.resolution),
    )
    digest = write_json_atomic(args.output, package)
    _ok(
        {
            "output_sha256": digest,
            "review_rows": len(package["review_rows"]),
            "auto_resolution_proposals": len(package["auto_resolution_proposals"]),
        }
    )
    return 0


def command_pilot_find_mentions(args: argparse.Namespace) -> int:
    """Experimental one-body pilot. Does not publish a versioned artifact."""
    snapshot = read_json(args.documents)
    identity = read_json(args.identity)
    selected = displayed_body_for_item(
        snapshot,
        args.archive_item_id,
        args.evidence_source,
    )
    result = run_mention_pilot(
        body_text=selected["text"],
        archive_item_id=args.archive_item_id,
        evidence_source=selected["evidence_source"],
        text_roles=selected["text_roles"],
        identity_snapshot=identity,
        detector=GeminiMentionDetector(),
    )
    digest = write_json_atomic(args.output, result)
    _ok(
        {
            "output_sha256": digest,
            "kind": result["kind"],
            "candidates": len(result["candidates"]),
            "unaligned_gemini_candidates": len(result["unaligned_gemini_candidates"]),
            "provider_calls": len(result["provider_calls"]),
        }
    )
    return 0


def command_find_mentions(args: argparse.Namespace) -> int:
    """Publish mention-proposals-v1 for the whole discovery selection.

    The diagnostic sidecar is attempted only after the proposal file exists.
    A sidecar failure leaves that proposal in place.
    """
    detector = GeminiSemanticDetector()
    snapshot = read_json(args.documents)
    discovery = read_json(args.discovery)
    identity = read_json(args.identity)
    try:
        published = publish_mention_findings(
            snapshot,
            discovery,
            identity,
            run_id=args.run_id,
            document_snapshot_file_sha256=sha256_file(args.documents),
            discovery_file_sha256=sha256_file(args.discovery),
            identity_snapshot_file_sha256=sha256_file(args.identity),
            detector=detector,
            proposal_output=args.output,
            diagnostics_output=args.diagnostics,
        )
    except DiagnosticsPublicationFailed as exc:
        print(
            json.dumps(
                {
                    "status": "PROPOSAL_PUBLISHED",
                    "error_code": "DIAGNOSTICS_PUBLICATION_FAILED",
                    "proposal_output": exc.proposal_output,
                    "proposal_sha256": exc.proposal_sha256,
                    "proposal_logical_sha256": exc.proposal_logical_sha256,
                    "diagnostics_published": False,
                    "reason": str(exc),
                },
                ensure_ascii=False,
            )
        )
        return 2
    _ok(
        {
            "proposal_output": str(args.output),
            "proposal_sha256": published.proposal_sha256,
            "proposal_logical_sha256": published.proposal_logical_sha256,
            "diagnostics_output": str(args.diagnostics),
            "diagnostics_sha256": published.diagnostics_sha256,
            "diagnostics_logical_sha256": published.diagnostics_logical_sha256,
            "diagnostics_published": True,
            "mention_count": published.mention_count,
        }
    )
    return 0


def command_package_mention_proposals(args: argparse.Namespace) -> int:
    """Publish mention-proposals-v1 from offsets. Does not call a provider."""
    snapshot = read_json(args.documents)
    discovery = read_json(args.discovery)
    packaged = package_mention_proposals(
        snapshot,
        discovery,
        read_json(args.geometry),
        run_id=args.run_id,
        document_snapshot_file_sha256=sha256_file(args.documents),
        discovery_file_sha256=sha256_file(args.discovery),
    )
    digest = write_json_atomic(args.output, packaged)
    _ok(
        {
            "output_sha256": digest,
            "logical_content_sha256": packaged["logical_content_sha256"],
            "schema_version": packaged["schema_version"],
            "mention_count": len(packaged["mentions"]),
        }
    )
    return 0


def command_checkpoint(args: argparse.Namespace) -> int:
    inputs = _bindings(args.input or [])
    outputs = _bindings(args.record or [])
    processed = None
    if args.processed_from:
        evidence = read_json(args.processed_from)
        verify_generated_artifact(evidence, EVIDENCE_PACKAGE_SCHEMA, "evidence package")
        if evidence.get("run_id") != args.run_id:
            raise RunIdMismatchError(
                "processed evidence run_id does not match checkpoint --run-id"
            )
        processed = processed_items_from_evidence(evidence)
    if args.prior:
        checkpoint = append_step(
            read_json(args.prior),
            step=args.step,
            input_hashes=inputs,
            output_hashes=outputs,
            processed_items=processed,
        )
        if checkpoint["run_id"] != args.run_id:
            raise RunIdMismatchError("checkpoint run_id does not match --run-id")
    else:
        checkpoint = initial_checkpoint(
            run_id=args.run_id,
            step=args.step,
            input_hashes=inputs,
            output_hashes=outputs,
            processed_items=processed,
        )
    digest = write_json_atomic(args.output, checkpoint)
    _ok({"output_sha256": digest, "completed_steps": checkpoint["completed_steps"]})
    return 0


def _ok(payload: dict) -> None:
    print(json.dumps({"status": "OK", **payload}, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vs-archive-people")
    commands = parser.add_subparsers(dest="command", required=True)

    discover = commands.add_parser("discover")
    discover.add_argument("--documents", type=Path, required=True)
    discover.add_argument("--run-id", required=True)
    discover.add_argument("--prior-checkpoint", type=Path)
    discover.add_argument("--output", type=Path, required=True)
    discover.set_defaults(func=command_discover)

    freeze = commands.add_parser(
        "freeze-evidence",
        help="Validate mention proposals and publish an immutable evidence package. Does not find names.",
    )
    freeze.add_argument("--documents", type=Path, required=True)
    freeze.add_argument("--discovery", type=Path, required=True)
    freeze.add_argument("--mentions", type=Path, required=True)
    freeze.add_argument("--run-id", required=True)
    freeze.add_argument("--output", type=Path, required=True)
    freeze.set_defaults(func=command_freeze_evidence)

    freeze_v2 = commands.add_parser(
        "freeze-evidence-v2",
        help=(
            "Verify mention-proposals-v1 against displayed text and publish "
            "evidence-package-v2. Not a pipeline step. Does not read diagnostics."
        ),
    )
    freeze_v2.add_argument("--documents", type=Path, required=True)
    freeze_v2.add_argument("--discovery", type=Path, required=True)
    freeze_v2.add_argument("--mentions", type=Path, required=True)
    freeze_v2.add_argument("--run-id", required=True)
    freeze_v2.add_argument("--output", type=Path, required=True)
    freeze_v2.set_defaults(func=command_freeze_evidence_v2)

    resolve = commands.add_parser("resolve")
    resolve.add_argument("--evidence", type=Path, required=True)
    resolve.add_argument("--identity", type=Path, required=True)
    resolve.add_argument("--local-candidates", type=Path)
    resolve.add_argument("--output", type=Path, required=True)
    resolve.set_defaults(func=command_resolve)

    review = commands.add_parser("review-package")
    review.add_argument("--resolution", type=Path, required=True)
    review.add_argument("--output", type=Path, required=True)
    review.set_defaults(func=command_review)

    pilot = commands.add_parser(
        "pilot-find-mentions",
        help=(
            "Experimental Gemini person-name span pilot for one displayed body. "
            "Not a pipeline step, not a versioned artifact, and not a production write."
        ),
    )
    pilot.add_argument("--documents", type=Path, required=True)
    pilot.add_argument("--archive-item-id", type=int, required=True)
    pilot.add_argument("--evidence-source")
    pilot.add_argument("--identity", type=Path, required=True)
    pilot.add_argument("--output", type=Path, required=True)
    pilot.set_defaults(func=command_pilot_find_mentions)

    package = commands.add_parser(
        "package-mention-proposals",
        help=(
            "Publish mention-proposals-v1 from human mention geometry. "
            "Not a pipeline step and not a provider call."
        ),
    )
    package.add_argument("--documents", type=Path, required=True)
    package.add_argument("--discovery", type=Path, required=True)
    package.add_argument("--geometry", type=Path, required=True)
    package.add_argument("--run-id", required=True)
    package.add_argument("--output", type=Path, required=True)
    package.set_defaults(func=command_package_mention_proposals)

    finder = commands.add_parser(
        "find-mentions",
        help=(
            "Publish mention-proposals-v1 for a discovery selection. "
            "Not a pipeline step. Does not assign production identity."
        ),
    )
    finder.add_argument("--documents", type=Path, required=True)
    finder.add_argument("--discovery", type=Path, required=True)
    finder.add_argument("--identity", type=Path, required=True)
    finder.add_argument("--run-id", required=True)
    finder.add_argument("--output", type=Path, required=True)
    finder.add_argument("--diagnostics", type=Path, required=True)
    finder.set_defaults(func=command_find_mentions)

    checkpoint = commands.add_parser("checkpoint")
    checkpoint.add_argument("--run-id", required=True)
    checkpoint.add_argument("--step", required=True, choices=step_chain())
    checkpoint.add_argument("--prior", type=Path)
    checkpoint.add_argument("--input", action="append", default=[])
    checkpoint.add_argument("--record", action="append", default=[])
    checkpoint.add_argument("--processed-from", type=Path)
    checkpoint.add_argument("--output", type=Path, required=True)
    checkpoint.set_defaults(func=command_checkpoint)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ArtifactError as exc:
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "error_code": exc.error_code,
                    "reason": str(exc),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
