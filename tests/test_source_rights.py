"""Tests for the hard source-rights gate and owner-media discovery."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from features.source_discovery import discover_owner_media
from features.source_rights import (
    BASIS_CREATIVE_COMMONS,
    BASIS_CREATOR_ALLOWLIST,
    BASIS_CREATOR_PERMISSION,
    BASIS_OWNER_MEDIA,
    BASIS_UNKNOWN,
    SourcePolicy,
    attach_rights,
    can_download,
    can_publish,
    evaluate_source,
)


class RightsGateTests(unittest.TestCase):
    def test_public_visibility_is_not_permission(self):
        decision = evaluate_source(
            {"video_id": "v1", "source_type": "youtube", "privacy_status": "public",
             "views": 1_000_000}
        )
        self.assertFalse(decision.allowed)
        self.assertFalse(decision.verified)
        self.assertEqual(decision.basis, BASIS_UNKNOWN)

    def test_owner_media_requires_confirmation(self):
        rejected = evaluate_source({"source_type": "owner_media", "path": "/tmp/a.mp4"})
        self.assertFalse(rejected.allowed)

        allowed = evaluate_source(
            {"source_type": "owner_media", "path": "/tmp/a.mp4", "rights_confirmed": True}
        )
        self.assertTrue(allowed.allowed)
        self.assertEqual(allowed.basis, BASIS_OWNER_MEDIA)

    def test_explicit_creator_permission(self):
        decision = evaluate_source(
            {"source_type": "youtube", "video_id": "v2", "explicit_permission": True}
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.basis, BASIS_CREATOR_PERMISSION)

    def test_creative_commons_license(self):
        decision = evaluate_source(
            {"source_type": "youtube", "video_id": "v3", "license": "creativeCommon"}
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.basis, BASIS_CREATIVE_COMMONS)
        self.assertIn("Attribution", decision.notes)

    def test_allowlist_matches_channel_id_or_url(self):
        policy = SourcePolicy(allowlist=("My Creator", "UC123", "https://youtu.be/abc"))
        for source in (
            {"channel": "my creator"},
            {"channel_id": "uc123"},
            {"url": "https://youtu.be/abc"},
        ):
            with self.subTest(source=source):
                decision = evaluate_source(source, policy)
                self.assertTrue(decision.allowed)
                self.assertEqual(decision.basis, BASIS_CREATOR_ALLOWLIST)

    def test_download_needs_flag_for_remote_media(self):
        source = {"source_type": "youtube", "video_id": "v4", "explicit_permission": True}
        decision = evaluate_source(source)
        self.assertFalse(can_download(decision, source, SourcePolicy(allow_download=False)))
        self.assertTrue(can_download(decision, source, SourcePolicy(allow_download=True)))

    def test_owner_media_needs_no_download_flag(self):
        source = {
            "source_type": "owner_media",
            "path": "/tmp/a.mp4",
            "rights_confirmed": True,
        }
        decision = evaluate_source(source)
        self.assertTrue(can_download(decision, source, SourcePolicy(allow_download=False)))

    def test_unverified_decision_can_never_be_published(self):
        decision = evaluate_source({"source_type": "youtube", "video_id": "v5"})
        self.assertFalse(can_publish(decision))

    def test_attach_rights_splits_eligible_and_rejected(self):
        sources = [
            {"video_id": "ok", "source_type": "youtube", "license": "creativeCommon"},
            {"video_id": "no", "source_type": "youtube"},
        ]
        eligible, rejected = attach_rights(sources)
        self.assertEqual([s["video_id"] for s in eligible], ["ok"])
        self.assertEqual([s["video_id"] for s in rejected], ["no"])
        self.assertTrue(eligible[0]["rights"]["verified"])
        self.assertFalse(rejected[0]["rights"]["verified"])


class OwnerMediaDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _media(self, name: str = "source.mp4") -> Path:
        path = self.root / name
        path.write_bytes(b"not-a-real-video")
        return path

    def test_confirmed_sidecar_is_discovered(self):
        path = self._media()
        (self.root / f"{path.name}.rights.json").write_text(
            json.dumps(
                {
                    "confirmed": True,
                    "title": "Owner clip",
                    "creator": "me",
                    "description": "own footage",
                }
            ),
            encoding="utf-8",
        )
        found = discover_owner_media(self.root)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["source_type"], "owner_media")
        self.assertTrue(found[0]["rights_confirmed"])
        self.assertEqual(found[0]["title"], "Owner clip")

    def test_missing_or_unconfirmed_sidecar_is_skipped(self):
        path = self._media()
        self.assertEqual(discover_owner_media(self.root), [])

        (self.root / f"{path.name}.rights.json").write_text(
            json.dumps({"confirmed": False}), encoding="utf-8"
        )
        self.assertEqual(discover_owner_media(self.root), [])

    def test_non_media_files_are_ignored(self):
        (self.root / "notes.txt").write_text("hello", encoding="utf-8")
        self.assertEqual(discover_owner_media(self.root), [])

    def test_missing_directory_is_safe(self):
        self.assertEqual(discover_owner_media(self.root / "nope"), [])


if __name__ == "__main__":
    unittest.main()
