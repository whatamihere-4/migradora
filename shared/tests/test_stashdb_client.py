"""Tests for StashDB client."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from migradora.stashdb_client import (
    StashdbClient,
    _flatten_fingerprint_matches,
    _extension_from_response,
    resolve_stashdb_metadata,
)


class FlattenFingerprintTests(unittest.TestCase):
    def test_flattens_nested_lists(self) -> None:
        raw = [[{"id": "a", "title": "One"}, {"id": "b", "title": "Two"}]]
        flat = _flatten_fingerprint_matches(raw)
        self.assertEqual(len(flat), 2)
        self.assertEqual(flat[0]["id"], "a")


class ExtensionFromResponseTests(unittest.TestCase):
    def test_content_type_webp(self) -> None:
        self.assertEqual(_extension_from_response("http://x/y", "image/webp"), ".webp")

    def test_url_suffix(self) -> None:
        self.assertEqual(
            _extension_from_response("http://x/cover.jpg", None),
            ".jpg",
        )


class StashdbClientTests(unittest.TestCase):
    def test_find_by_oshash_disabled_without_key(self) -> None:
        client = StashdbClient("")
        self.assertIsNone(client.find_by_oshash("abc123"))

    def test_find_by_oshash_returns_match(self) -> None:
        client = StashdbClient("key")
        with patch.object(
            client,
            "_post",
            return_value={
                "findScenesBySceneFingerprints": [
                    [{"id": "scene-1", "title": "My Scene"}]
                ]
            },
        ):
            match = client.find_by_oshash("deadbeef")
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.scene_id, "scene-1")
        self.assertEqual(match.title, "My Scene")


class ResolveStashdbMetadataTests(unittest.TestCase):
    def test_skips_lookup_when_cached(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cover = Path(tmp) / ".stashdb-cover.jpg"
            cover.write_bytes(b"img")
            client = MagicMock()
            sid, title, path = resolve_stashdb_metadata(
                client,
                "hash",
                Path(tmp),
                existing_scene_id="s1",
                existing_title="Cached Title",
                existing_cover_path=str(cover),
            )
            self.assertEqual(title, "Cached Title")
            self.assertEqual(path, str(cover))
            client.find_by_oshash.assert_not_called()


if __name__ == "__main__":
    unittest.main()
