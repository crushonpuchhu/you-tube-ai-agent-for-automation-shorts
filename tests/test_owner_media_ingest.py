"""Tests for Telegram owner-media ingest (no Telegram or FFmpeg needed)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from features.source_discovery import discover_owner_media
from features.source_rights import evaluate_source
from features.telegram_autonomous_hooks import ingest_owner_media


class IngestOwnerMediaTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.upload = self.root / "upload_deadbeef.mp4"
        self.upload.write_bytes(b"\x00" * 1024)

    def tearDown(self):
        self._tmp.cleanup()

    def test_moves_file_and_writes_confirming_sidecar(self):
        dest = self.root / "sources"
        path, sidecar = ingest_owner_media(
            dest, self.upload, title="Long gym tutorial", creator="owner"
        )

        self.assertTrue(path.is_file())
        self.assertTrue(sidecar.is_file())
        self.assertFalse(self.upload.exists(), "upload must be moved, not copied")
        self.assertEqual(path.parent, dest)
        self.assertTrue(path.name.endswith(".mp4"))

        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertTrue(payload["confirmed"])
        self.assertEqual(payload["rights_basis"], "owner_media")
        self.assertEqual(payload["title"], "Long gym tutorial")

    def test_ingested_file_is_discovered_and_passes_the_rights_gate(self):
        dest = self.root / "sources"
        ingest_owner_media(dest, self.upload, title="Own footage")

        found = discover_owner_media(dest)
        self.assertEqual(len(found), 1)
        decision = evaluate_source(found[0])
        self.assertTrue(decision.allowed)
        self.assertTrue(decision.verified)
        self.assertEqual(decision.basis, "owner_media")

    def test_missing_upload_raises(self):
        with self.assertRaises(FileNotFoundError):
            ingest_owner_media(self.root / "sources", self.root / "nope.mp4")


if __name__ == "__main__":
    unittest.main()
