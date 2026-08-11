"""Unit tests for Filester folder lookup and create conflict handling."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import httpx

from migradora.filester_client import FilesterClient, FilesterFolder, FolderIndex


class FolderIndexTests(unittest.TestCase):
    def test_find_child_by_parent_db_id(self) -> None:
        index = FolderIndex([
            FilesterFolder(identifier="vr", name="VR", db_id=10, parent_db_id=None),
            FilesterFolder(identifier="czech", name="CzechVR", db_id=11, parent_db_id=10),
        ])
        hit = index.find_child("CzechVR", parent_db_id=10)
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit.identifier, "czech")

    def test_find_child_by_parent_identifier(self) -> None:
        index = FolderIndex([
            FilesterFolder(identifier="vr", name="VR", db_id=10, parent_db_id=None),
            FilesterFolder(identifier="czech", name="CzechVR", db_id=11, parent_db_id=10),
        ])
        hit = index.find_child("CzechVR", parent_identifier="vr")
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit.identifier, "czech")

    def test_find_child_name_fallback_root(self) -> None:
        index = FolderIndex([
            FilesterFolder(identifier="czech", name="CzechVR", db_id=11, parent_db_id=None),
        ])
        hit = index.find_child("CzechVR")
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit.identifier, "czech")

    def test_find_child_ambiguous_same_name(self) -> None:
        index = FolderIndex([
            FilesterFolder(identifier="a", name="Clips", db_id=1, parent_db_id=None),
            FilesterFolder(identifier="b", name="Clips", db_id=2, parent_db_id=5),
        ])
        self.assertIsNone(index.find_child("Clips"))


class CreateFolderConflictTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FilesterClient("test-key")

    def tearDown(self) -> None:
        self.client.close()

    def test_create_returns_existing_on_409(self) -> None:
        existing = FilesterFolder(identifier="czech-id", name="CzechVR")

        conflict = httpx.Response(
            409,
            request=httpx.Request("POST", "https://u1.filester.me/api/v1/folder"),
            json={"success": False, "message": "You already have a folder with this name"},
        )

        with patch.object(self.client, "find_folder", return_value=existing):
            with patch.object(self.client, "_post_folder", side_effect=httpx.HTTPStatusError(
                "conflict", request=conflict.request, response=conflict
            )):
                folder = self.client.create_folder("CzechVR")
        self.assertEqual(folder.identifier, "czech-id")

    def test_nested_create_409_raises_when_root_duplicate(self) -> None:
        root = FilesterFolder(identifier="root-czech", name="CzechVR")
        conflict = httpx.Response(
            409,
            request=httpx.Request("POST", "https://u1.filester.me/api/v1/folder"),
            json={
                "success": False,
                "message": "DUPLICATE_NAME",
                "data": {"identifier": "root-czech"},
            },
        )

        def find_side_effect(name: str, **kwargs: object) -> FilesterFolder | None:
            if kwargs.get("parent_identifier"):
                return None
            if name == "CzechVR":
                return root
            return None

        with patch.object(self.client, "find_folder", side_effect=find_side_effect):
            with patch.object(self.client, "_post_folder", side_effect=httpx.HTTPStatusError(
                "conflict", request=conflict.request, response=conflict
            )):
                with self.assertRaises(RuntimeError) as ctx:
                    self.client.create_folder("CzechVR", parent_identifier="vr-id")
        self.assertIn("top-level", str(ctx.exception).lower())

    def test_assert_nested_folder_rejects_root_match(self) -> None:
        root = FilesterFolder(identifier="root-id", name="CzechVR")
        with patch.object(self.client, "list_child_folders", return_value=[]):
            with patch.object(self.client, "find_folder", return_value=root):
                with self.assertRaises(RuntimeError):
                    self.client.assert_nested_folder(
                        root,
                        "CzechVR",
                        parent_identifier="vr-id",
                    )

    def test_parse_create_response_parent(self) -> None:
        folder = FilesterClient._parse_folder_from_create({
            "success": True,
            "data": {
                "identifier": "8198067eaca26584",
                "name": "migradora-nested-test",
                "parent": "558b65a42fdad1f6",
            },
        })
        self.assertIsNotNone(folder)
        assert folder is not None
        self.assertEqual(folder.parent_identifier, "558b65a42fdad1f6")

    def test_nested_create_does_not_reuse_root_folder(self) -> None:
        create_body = {
            "success": True,
            "data": {
                "identifier": "nested-id",
                "name": "CzechVR",
                "parent": "vr-id",
            },
        }

        with patch.object(self.client, "find_folder") as find_folder:
            with patch.object(self.client, "_find_folder_under_parent", return_value=None):
                with patch.object(
                    self.client,
                    "_resolve_existing_nested_folder",
                    return_value=None,
                ):
                    with patch.object(self.client, "_post_folder", return_value=create_body):
                        folder = self.client.create_folder(
                            "CzechVR",
                            parent_identifier="vr-id",
                        )
        self.assertEqual(folder.identifier, "nested-id")
        self.assertEqual(folder.parent_identifier, "vr-id")
        find_folder.assert_not_called()

    def test_nested_create_reuses_existing_under_parent_on_409(self) -> None:
        existing = FilesterFolder(
            identifier="nested-id",
            name="CzechVR",
            parent_identifier="vr-id",
        )

        conflict = httpx.Response(
            409,
            request=httpx.Request("POST", "https://u1.filester.me/api/v1/folder"),
            json={
                "success": False,
                "message": "You already have a folder with this name here",
                "error": "DUPLICATE_NAME",
            },
        )

        with patch.object(
            self.client,
            "_resolve_existing_nested_folder",
            return_value=existing,
        ):
            with patch.object(self.client, "_post_folder", side_effect=httpx.HTTPStatusError(
                "conflict", request=conflict.request, response=conflict
            )):
                folder = self.client.create_folder("CzechVR", parent_identifier="vr-id")
        self.assertEqual(folder.identifier, "nested-id")
        self.assertEqual(folder.parent_identifier, "vr-id")

    def test_parse_folder_keeps_hex_identifier_separate_from_db_id(self) -> None:
        folder = FilesterClient._parse_folder({
            "id": "a1b2c3d4e5f6",
            "name": "VR",
            "parent_id": 0,
        })
        self.assertIsNotNone(folder)
        assert folder is not None
        self.assertEqual(folder.identifier, "a1b2c3d4e5f6")
        self.assertIsNone(folder.db_id)

    def test_parse_folder_numeric_id(self) -> None:
        folder = FilesterClient._parse_folder({
            "id": 42,
            "identifier": "hexslug",
            "name": "CzechVR",
            "parent_id": 10,
        })
        self.assertIsNotNone(folder)
        assert folder is not None
        self.assertEqual(folder.db_id, 42)
        self.assertEqual(folder.identifier, "hexslug")
        self.assertEqual(folder.parent_db_id, 10)

    def test_file_identifier_from_response_slug(self) -> None:
        self.assertEqual(
            FilesterClient.file_identifier_from_response({"slug": "abc123"}),
            "abc123",
        )

    def test_move_files_payload(self) -> None:
        with patch.object(self.client, "_request", return_value={"success": True, "data": {"moved": 2}}) as req:
            data = self.client.move_files(["a", "b"], "folder-id")
        self.assertEqual(data["moved"], 2)
        req.assert_called_once_with(
            "POST",
            "/api/v1/files/move",
            json={"files": ["a", "b"], "folder": "folder-id"},
        )


class VerifyUploadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FilesterClient(
            "test-key",
            verify_upload_max_wait_sec=10,
            verify_upload_poll_sec=0.01,
            verify_upload_request_timeout_sec=5,
        )

    def tearDown(self) -> None:
        self.client.close()

    def test_verify_waits_until_file_visible(self) -> None:
        responses = [
            {"error": "Database error"},
            {"data": {"size": 0}},
            {"data": {"size": 1000, "slug": "slug1"}},
        ]
        with patch.object(self.client, "_api_request_once", side_effect=responses) as req:
            ok = self.client.verify_upload("slug1", 1000)
        self.assertTrue(ok)
        self.assertGreaterEqual(req.call_count, 2)

    def test_verify_fails_on_terminal_status(self) -> None:
        with patch.object(
            self.client,
            "_api_request_once",
            return_value={"status": "failed"},
        ):
            ok = self.client.verify_upload("slug1", 1000)
        self.assertFalse(ok)

    def test_verify_uses_file_api_when_status_errors(self) -> None:
        with patch.object(
            self.client,
            "_api_request_once",
            side_effect=[
                {"error": "Database error"},
                {"data": {"size": 1000, "slug": "slug1"}},
            ],
        ) as req:
            ok = self.client.verify_upload("slug1", 1000)
        self.assertTrue(ok)
        self.assertEqual(req.call_count, 2)

    def test_verify_fails_on_size_mismatch_when_strict(self) -> None:
        strict = FilesterClient("test-key", verify_upload_strict=True)
        try:
            with patch.object(
                strict,
                "_api_request_once",
                side_effect=[
                    {"status": "completed"},
                    {"data": {"size": 500}},
                ],
            ):
                ok = strict.verify_upload("slug1", 1000)
            self.assertFalse(ok)
        finally:
            strict.close()

    def test_verify_trusts_upload_on_timeout_by_default(self) -> None:
        with patch.object(
            self.client,
            "_api_request_once",
            side_effect=httpx.ReadTimeout("slow"),
        ):
            ok = self.client.verify_upload("slug1", 1000)
        self.assertTrue(ok)

    def test_verify_skipped_when_disabled(self) -> None:
        disabled = FilesterClient("test-key", verify_upload_enabled=False)
        try:
            with patch.object(disabled, "_api_request_once") as req:
                ok = disabled.verify_upload("slug1", 1000)
            self.assertTrue(ok)
            req.assert_not_called()
        finally:
            disabled.close()


class UploadRetryDuplicateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FilesterClient("test-key", max_retries=3, retry_delay=0)

    def tearDown(self) -> None:
        self.client.close()

    def test_no_retry_after_full_upload_when_response_lost(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(b"hello")
            path = Path(tmp.name)
        try:
            post = MagicMock(side_effect=httpx.ReadTimeout("slow response"))
            with patch.object(self.client._client, "post", post):
                with patch.object(
                    self.client,
                    "_recover_upload_response",
                    return_value=None,
                ):
                    with self.assertRaises(RuntimeError) as ctx:
                        self.client.upload_file(path, folder_id="folder-1")
            self.assertIn("duplicate", str(ctx.exception).lower())
            self.assertEqual(post.call_count, 1)
        finally:
            path.unlink(missing_ok=True)

    def test_recovers_slug_after_response_lost(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(b"hello")
            path = Path(tmp.name)
        try:
            with patch.object(
                self.client._client,
                "post",
                side_effect=httpx.ReadTimeout("slow"),
            ):
                with patch.object(
                    self.client,
                    "_recover_upload_response",
                    return_value={"slug": "recovered"},
                ):
                    result = self.client.upload_file(path, folder_id="folder-1")
            self.assertEqual(result.get("slug"), "recovered")
        finally:
            path.unlink(missing_ok=True)


class UploadFolderThumbnailTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FilesterClient("test-key")

    def tearDown(self) -> None:
        self.client.close()

    def test_upload_folder_thumbnail_posts_multipart(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp.write(b"jpegdata")
            path = Path(tmp.name)
        try:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.content = b'{"success": true}'
            mock_resp.json.return_value = {"success": True}
            with patch.object(self.client._client, "post", return_value=mock_resp) as post:
                result = self.client.upload_folder_thumbnail("folder-id", path)
            self.assertTrue(result.get("success"))
            post.assert_called_once()
            call_kwargs = post.call_args
            self.assertEqual(call_kwargs[0][0], "/api/v1/folder/thumbnail")
            self.assertIn("files", call_kwargs[1])
            self.assertIn("data", call_kwargs[1])
            self.assertEqual(call_kwargs[1]["data"]["folder"], "folder-id")
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
