"""Tests for HTTP download helper (Real-Debrid CDN behaviour)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

from migradora.http_download import DOWNLOAD_HEADERS, download_url, expired_link_error


class _StreamResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        url: str = "https://cdn.example.com/d/x/file.mp4",
        content_length: str | None = "4",
    ) -> None:
        self.status_code = status_code
        self.url = url
        self.headers = {}
        if content_length is not None:
            self.headers["content-length"] = content_length

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "error",
                request=httpx.Request("GET", self.url),
                response=httpx.Response(self.status_code, request=httpx.Request("GET", self.url)),
            )

    def iter_bytes(self, chunk_size: int = 1024) -> bytes:
        yield b"test"

    def __enter__(self) -> _StreamResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


class HttpDownloadTests(unittest.TestCase):
    def test_expired_link_error_message(self) -> None:
        err = expired_link_error(403, "https://45.download.real-debrid.cloud/d/x/file.mp4")
        self.assertIn("403", str(err))
        self.assertIn("panel link", str(err))

    def test_download_sends_browser_headers(self) -> None:
        client = MagicMock()
        client.stream.return_value = _StreamResponse()
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "movie.mp4"
            download_url(client, "https://cdn.example.com/d/x/file.mp4", dest)
            _, kwargs = client.stream.call_args
            headers = kwargs["headers"]
            self.assertEqual(headers["User-Agent"], DOWNLOAD_HEADERS["User-Agent"])
            self.assertEqual(headers["Accept-Encoding"], "identity")

    def test_download_raises_on_expired_status(self) -> None:
        client = MagicMock()
        client.stream.return_value = _StreamResponse(status_code=403)
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "movie.mp4"
            with self.assertRaises(RuntimeError) as ctx:
                download_url(client, "https://cdn.example.com/d/x/file.mp4", dest)
            self.assertIn("expired", str(ctx.exception).lower())

    def test_skip_check_runs_after_stream_error(self) -> None:
        client = MagicMock()
        client.stream.side_effect = httpx.ReadTimeout("stall")
        calls: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "movie.mp4"
            with self.assertRaises(httpx.ReadTimeout):
                download_url(
                    client,
                    "https://cdn.example.com/d/x/file.mp4",
                    dest,
                    skip_check=lambda: calls.append("skip"),
                )
        self.assertIn("skip", calls)


class RealDebridResolveTests(unittest.TestCase):
    def test_panel_link_is_unrestricted(self) -> None:
        from migradora.config import Settings
        from migradora.realdebrid_client import RealDebridClient

        settings = Settings(
            real_debrid_api_token="token",
            real_debrid_preferred_cdn="",
        )
        panel = "https://real-debrid.com/d/AbCdEf123/"
        logs: list[str] = []

        with patch.object(RealDebridClient, "unrestrict_link") as mock_unrestrict:
            mock_unrestrict.return_value = {
                "download": "https://nyk7-4.download.real-debrid.com/d/XYZ/file.mp4",
                "filename": "file.mp4",
                "filesize": 1234,
            }
            with RealDebridClient(settings) as rd:
                meta = rd.resolve_metadata(panel, on_log=logs.append)

        mock_unrestrict.assert_called_once()
        self.assertTrue(meta["resolved"])
        self.assertIn("nyk7-4", meta["download_url"])
        self.assertTrue(any("Unrestricting" in line for line in logs))

    def test_cdn_link_is_not_re_unrestricted(self) -> None:
        from migradora.config import Settings
        from migradora.realdebrid_client import RealDebridClient

        settings = Settings(real_debrid_api_token="token")
        cdn = "https://nyk7-4.download.real-debrid.com/d/OLD/file.mp4"
        logs: list[str] = []

        with patch.object(RealDebridClient, "unrestrict_link") as mock_unrestrict:
            with RealDebridClient(settings) as rd:
                meta = rd.resolve_metadata(cdn, on_log=logs.append)

        mock_unrestrict.assert_not_called()
        self.assertFalse(meta["resolved"])
        self.assertTrue(any("Stored CDN URL" in line for line in logs))

    def test_hoster_unavailable_falls_back_to_downloads_cache(self) -> None:
        from migradora.config import Settings
        from migradora.realdebrid_client import RealDebridClient, RealDebridError

        settings = Settings(real_debrid_api_token="token")
        panel = "https://real-debrid.com/d/AbCdEf123/"
        name = "VRxResident Evil 4 A XXX Parody.mp4"
        logs: list[str] = []

        with patch.object(
            RealDebridClient,
            "unrestrict_link",
            side_effect=RealDebridError("Real-Debrid API HTTP 503: hoster_unavailable"),
        ):
            with patch.object(
                RealDebridClient,
                "find_cached_download",
                return_value={
                    "download_url": "https://nyk7-4.download.real-debrid.com/d/X/file.mp4",
                    "filename": name,
                    "filesize": 999,
                    "resolved": True,
                    "source": "downloads_cache",
                },
            ) as mock_cache:
                with RealDebridClient(settings) as rd:
                    meta = rd.resolve_metadata(
                        panel,
                        filename_hint=name,
                        on_log=logs.append,
                    )

        mock_cache.assert_called_once()
        self.assertEqual(mock_cache.call_args.args[0], name)
        self.assertEqual(meta["source"], "downloads_cache")
        self.assertTrue(any("/downloads cache" in line for line in logs))
