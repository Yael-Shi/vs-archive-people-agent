import json
import unittest
from pathlib import Path

from vs_archive_people.evidence.text_bodies import (
    displayed_text_sha256,
    select_text_bodies,
)

FIXTURE = Path(__file__).parent / "fixtures" / "document_text_snapshot.json"


class TextBodyTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.ocr = self.snapshot["items"][0]
        self.manual = self.snapshot["items"][1]

    def test_identical_raw_transcription_and_source_merge_roles(self):
        bodies = select_text_bodies(self.ocr)
        merged = bodies[0]
        self.assertEqual(
            merged["text_roles"],
            [
                "ocr_document.displayed_transcription.text",
                "ocr_document.displayed_source_text.text",
            ],
        )
        self.assertIn("|", merged["text_role"])
        self.assertEqual(merged["text"], "Ada Lovelace wrote the note.")

    def test_whitespace_only_difference_stays_separate(self):
        item = json.loads(json.dumps(self.ocr))
        item["ocr_document"]["displayed_source_text"]["text"] = (
            "Ada Lovelace  wrote the note."
        )
        bodies = select_text_bodies(item)
        self.assertEqual(len(bodies), 3)

    def test_hebrew_translation_is_included(self):
        texts = [body["text"] for body in select_text_bodies(self.ocr)]
        self.assertIn("עדה לאבלייס כתבה את ההערה.", texts)

    def test_manual_body_is_included(self):
        bodies = select_text_bodies(self.manual)
        self.assertEqual(len(bodies), 1)
        self.assertEqual(bodies[0]["text_role"], "manual_text.body")
        self.assertEqual(bodies[0]["text"], "Grace Hopper served in the navy.")

    def test_blank_and_missing_fields_are_excluded(self):
        item = {
            "archive_item_id": 12,
            "item_type": "OCR_DOCUMENT",
            "ocr_document": {
                "displayed_transcription": {"text": "   "},
                "displayed_source_text": {"text": "Kept sentence."},
            },
        }
        bodies = select_text_bodies(item)
        self.assertEqual([body["text"] for body in bodies], ["Kept sentence."])

    def test_title_is_not_a_displayed_body(self):
        for item in (self.ocr, self.manual):
            for body in select_text_bodies(item):
                self.assertNotIn(item["title"], body["text"])
                self.assertNotIn("title", body["text_role"])

    def test_displayed_hash_ignores_timestamps_and_title(self):
        original = displayed_text_sha256(self.ocr)
        changed = json.loads(json.dumps(self.ocr))
        changed["updated_at"] = "2099-01-01T00:00:00+00:00"
        changed["created_at"] = "1999-01-01T00:00:00+00:00"
        changed["title"] = "A different metadata title"
        self.assertEqual(displayed_text_sha256(changed), original)

    def test_displayed_hash_changes_when_body_text_changes(self):
        original = displayed_text_sha256(self.ocr)
        changed = json.loads(json.dumps(self.ocr))
        changed["ocr_document"]["displayed_hebrew_translation"]["text"] = "נוסח אחר."
        self.assertNotEqual(displayed_text_sha256(changed), original)


if __name__ == "__main__":
    unittest.main()
