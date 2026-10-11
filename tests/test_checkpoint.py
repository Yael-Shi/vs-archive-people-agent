import json
import unittest
from pathlib import Path

from vs_archive_people.artifacts.io import (
    ArtifactError,
    IncompletePriorCheckpointError,
    MissingBoundInputError,
    UnsupportedSchemaVersionError,
    sha256_json,
)
from vs_archive_people.discovery.select import select_items
from vs_archive_people.runs.checkpoint import (
    InputHashChangedError,
    append_step,
    initial_checkpoint,
    processed_items_from_evidence,
)

DOCUMENTS = json.loads(
    (Path(__file__).parent / "fixtures" / "document_text_snapshot.json").read_text(
        encoding="utf-8"
    )
)
HASH = "a" * 64
OTHER = "b" * 64


def _resume(checkpoint, step, processed_items=None):
    return append_step(
        checkpoint,
        step=step,
        input_hashes=dict(checkpoint["input_file_hashes"]),
        output_hashes={},
        processed_items=processed_items,
    )


def _complete(checkpoint):
    current = checkpoint
    for step in ("freeze-evidence", "resolve", "review"):
        current = _resume(current, step)
    return current


class CheckpointTests(unittest.TestCase):
    def test_checkpoint_records_run_schema_and_artifact_hashes(self):
        checkpoint = initial_checkpoint(
            run_id="run-1",
            step="discover",
            input_hashes={"document_text_snapshot": HASH},
            output_hashes={"discovery_selection": OTHER},
        )
        self.assertEqual(checkpoint["schema_version"], "run-checkpoint-v1")
        self.assertEqual(checkpoint["run_id"], "run-1")
        self.assertIn("document_text_snapshot", checkpoint["schema_versions"])
        self.assertEqual(checkpoint["completed_steps"], ["discover"])
        self.assertEqual(
            checkpoint["input_file_hashes"]["document_text_snapshot"], HASH
        )

    def test_changed_input_hash_blocks_the_same_run(self):
        prior = initial_checkpoint(
            run_id="run-1",
            step="discover",
            input_hashes={"document_text_snapshot": HASH},
            output_hashes={"discovery_selection": OTHER},
        )
        with self.assertRaises(InputHashChangedError):
            append_step(
                prior,
                step="freeze-evidence",
                input_hashes={"document_text_snapshot": "c" * 64},
                output_hashes={"evidence_package": "d" * 64},
            )

    def test_same_inputs_can_record_the_next_step(self):
        prior = initial_checkpoint(
            run_id="run-1",
            step="discover",
            input_hashes={"document_text_snapshot": HASH},
            output_hashes={"discovery_selection": OTHER},
        )
        nxt = append_step(
            prior,
            step="freeze-evidence",
            input_hashes={
                "document_text_snapshot": HASH,
                "mention_proposals": "e" * 64,
            },
            output_hashes={"evidence_package": "d" * 64},
            processed_items=[
                {"archive_item_id": 10, "displayed_text_sha256": "f" * 64}
            ],
        )
        self.assertEqual(nxt["completed_steps"], ["discover", "freeze-evidence"])
        self.assertEqual(nxt["input_file_hashes"]["mention_proposals"], "e" * 64)
        self.assertEqual(nxt["processed_items"][0]["archive_item_id"], 10)

    def test_discovery_uses_processed_hashes_not_timestamps(self):
        first = select_items(DOCUMENTS)
        self.assertEqual(
            [row["archive_item_id"] for row in first["selected_items"]],
            [10, 11],
        )
        checkpoint = initial_checkpoint(
            run_id="run-1",
            step="discover",
            input_hashes={"document_text_snapshot": HASH},
            output_hashes={"evidence_package": OTHER},
            processed_items=[
                {
                    "archive_item_id": row["archive_item_id"],
                    "displayed_text_sha256": row["displayed_text_sha256"],
                }
                for row in first["selected_items"]
            ],
        )
        checkpoint = _complete(checkpoint)
        stamped = json.loads(json.dumps(DOCUMENTS))
        stamped["items"][0]["updated_at"] = "2099-12-31T00:00:00+00:00"
        again = select_items(stamped, checkpoint)
        self.assertEqual(again["selected_items"], [])

        changed = json.loads(json.dumps(DOCUMENTS))
        changed["items"][1]["manual_text"]["body"] = "Grace Hopper served at sea."
        selected = select_items(changed, checkpoint)
        self.assertEqual(len(selected["selected_items"]), 1)
        self.assertEqual(selected["selected_items"][0]["archive_item_id"], 11)
        self.assertEqual(
            selected["selected_items"][0]["reason"], "displayed_text_changed"
        )

    def test_only_a_review_complete_checkpoint_seeds_discovery(self):
        seen = next(
            row
            for row in select_items(DOCUMENTS)["selected_items"]
            if row["archive_item_id"] == 10
        )
        discover = initial_checkpoint(
            run_id="run-previous",
            step="discover",
            input_hashes={"document_text_snapshot": HASH},
            output_hashes={"discovery_selection": OTHER},
            processed_items=[
                {
                    "archive_item_id": 10,
                    "displayed_text_sha256": seen["displayed_text_sha256"],
                }
            ],
        )
        freeze = _resume(discover, "freeze-evidence")
        resolve = _resume(freeze, "resolve")
        review = _resume(resolve, "review")
        self.assertEqual(
            review["completed_steps"],
            ["discover", "freeze-evidence", "resolve", "review"],
        )
        self.assertNotEqual(review["run_id"], "run-new")
        for partial in (discover, freeze, resolve):
            with self.assertRaises(IncompletePriorCheckpointError) as caught:
                select_items(DOCUMENTS, partial)
            self.assertEqual(caught.exception.error_code, "INCOMPLETE_PRIOR_CHECKPOINT")
        resumed = _resume(resolve, "review")
        self.assertEqual(resumed["completed_steps"][-1], "review")
        seeded = select_items(DOCUMENTS, review)
        self.assertNotIn(
            10, [row["archive_item_id"] for row in seeded["selected_items"]]
        )

    def test_run_must_start_at_discover(self):
        for step in ("freeze-evidence", "resolve", "review", "extract"):
            with self.assertRaises(ArtifactError):
                initial_checkpoint(
                    run_id="run-1",
                    step=step,
                    input_hashes={"document_text_snapshot": HASH},
                    output_hashes={"discovery_selection": OTHER},
                )

    def test_cannot_skip_or_repeat_a_step(self):
        prior = initial_checkpoint(
            run_id="run-1",
            step="discover",
            input_hashes={"document_text_snapshot": HASH},
            output_hashes={"discovery_selection": OTHER},
        )
        with self.assertRaises(ArtifactError):
            append_step(
                prior,
                step="resolve",
                input_hashes={"document_text_snapshot": HASH},
                output_hashes={"resolution_report": "d" * 64},
            )
        with self.assertRaises(ArtifactError):
            append_step(
                prior,
                step="discover",
                input_hashes={"document_text_snapshot": HASH},
                output_hashes={"discovery_selection": OTHER},
            )
        broken = json.loads(json.dumps(prior))
        broken["completed_steps"] = ["discover", "discover"]
        with self.assertRaises(ArtifactError):
            append_step(
                broken,
                step="freeze-evidence",
                input_hashes={"document_text_snapshot": HASH},
                output_hashes={"evidence_package": "d" * 64},
            )

    def test_omitted_or_changed_input_and_schema_drift_fail(self):
        prior = initial_checkpoint(
            run_id="run-1",
            step="discover",
            input_hashes={"document_text_snapshot": HASH},
            output_hashes={"discovery_selection": OTHER},
        )
        with self.assertRaises(MissingBoundInputError) as missing:
            append_step(
                prior,
                step="freeze-evidence",
                input_hashes={"mention_proposals": "e" * 64},
                output_hashes={"evidence_package": "d" * 64},
            )
        self.assertIn("not provided", str(missing.exception))
        drifted = json.loads(json.dumps(prior))
        drifted["schema_versions"]["document_text_snapshot"] = (
            "document-text-snapshot-v0"
        )
        with self.assertRaises(UnsupportedSchemaVersionError) as unsupported:
            append_step(
                drifted,
                step="freeze-evidence",
                input_hashes={"document_text_snapshot": HASH},
                output_hashes={"evidence_package": "d" * 64},
            )
        self.assertEqual(unsupported.exception.error_code, "UNSUPPORTED_SCHEMA_VERSION")

    def test_uppercase_digest_is_accepted_and_stored_lowercase(self):
        checkpoint = initial_checkpoint(
            run_id="run-1",
            step="discover",
            input_hashes={"document_text_snapshot": "A" * 64},
            output_hashes={"discovery_selection": "B" * 64},
        )
        self.assertEqual(
            checkpoint["input_file_hashes"]["document_text_snapshot"], "a" * 64
        )

    def test_sequential_progression_preserves_schema_versions(self):
        discover = initial_checkpoint(
            run_id="run-1",
            step="discover",
            input_hashes={"document_text_snapshot": HASH},
            output_hashes={"discovery_selection": OTHER},
        )
        frozen = append_step(
            discover,
            step="freeze-evidence",
            input_hashes={
                "document_text_snapshot": HASH,
                "mention_proposals": "c" * 64,
            },
            output_hashes={"evidence_package": "d" * 64},
        )
        resolved = append_step(
            frozen,
            step="resolve",
            input_hashes={
                "document_text_snapshot": HASH,
                "mention_proposals": "c" * 64,
                "identity_snapshot": "e" * 64,
            },
            output_hashes={"resolution_report": "f" * 64},
        )
        reviewed = append_step(
            resolved,
            step="review",
            input_hashes={
                "document_text_snapshot": HASH,
                "mention_proposals": "c" * 64,
                "identity_snapshot": "e" * 64,
            },
            output_hashes={"identity_review_package": "1" * 64},
        )
        self.assertEqual(
            reviewed["completed_steps"],
            ["discover", "freeze-evidence", "resolve", "review"],
        )
        self.assertEqual(reviewed["schema_versions"], discover["schema_versions"])

    def test_processed_items_come_from_the_evidence_package(self):
        rows = processed_items_from_evidence(
            {
                "items": [
                    {
                        "archive_item_id": 10,
                        "displayed_text_sha256": sha256_json("body"),
                    },
                ]
            }
        )
        self.assertEqual(rows[0]["archive_item_id"], 10)


if __name__ == "__main__":
    unittest.main()
