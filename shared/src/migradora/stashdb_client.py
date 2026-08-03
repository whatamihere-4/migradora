"""StashDB GraphQL client — OSHASH scene lookup and cover image download."""

from __future__ import annotations

import logging
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger("migradora.stashdb")

_DEFAULT_GRAPHQL_URL = "https://stashdb.org/graphql"
_MAX_COVER_BYTES = 5 * 1024 * 1024

_FIND_BY_FINGERPRINTS_QUERY = """
query FindScenesBySceneFingerprints($fingerprints: [[FingerprintQueryInput!]!]!) {
  findScenesBySceneFingerprints(fingerprints: $fingerprints) {
    id
    title
    date
    duration
    urls { url }
    studio { name }
    performers {
      performer { name gender }
    }
    fingerprints {
      algorithm
      hash
      duration
    }
  }
}
"""

_SCENE_IMAGES_QUERY = """
query SceneImages($id: ID!) {
  findScene(id: $id) {
    id
    title
    images {
      id
      url
      width
      height
    }
  }
}
"""


@dataclass(frozen=True)
class StashdbSceneMatch:
    scene_id: str
    title: str


class StashdbClient:
    def __init__(
        self,
        api_key: str,
        graphql_url: str = _DEFAULT_GRAPHQL_URL,
        *,
        timeout: float = 25.0,
    ) -> None:
        self._api_key = (api_key or "").strip()
        self._graphql_url = (graphql_url or _DEFAULT_GRAPHQL_URL).strip()
        self._timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    def _post(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {"Content-Type": "application/json", "ApiKey": self._api_key}
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.post(
                self._graphql_url,
                json={"query": query, "variables": variables or {}},
                headers=headers,
            )
            resp.raise_for_status()
            payload = resp.json()
        if payload.get("errors"):
            raise RuntimeError(str(payload["errors"]))
        return payload.get("data") or {}

    def find_by_oshash(self, oshash: str) -> StashdbSceneMatch | None:
        """Return the first scene match for an OSHASH fingerprint, or None."""
        h = (oshash or "").strip().lower()
        if not h or not self.enabled:
            return None
        data = self._post(
            _FIND_BY_FINGERPRINTS_QUERY,
            {"fingerprints": [[{"algorithm": "OSHASH", "hash": h}]]},
        )
        raw = data.get("findScenesBySceneFingerprints")
        scenes = _flatten_fingerprint_matches(raw)
        if not scenes:
            return None
        scene = scenes[0]
        sid = str(scene.get("id") or "").strip()
        title = str(scene.get("title") or "").strip()
        if not sid or not title:
            return None
        return StashdbSceneMatch(scene_id=sid, title=title)

    def largest_image_url(self, scene_id: str) -> str | None:
        data = self._post(_SCENE_IMAGES_QUERY, {"id": scene_id})
        scene = data.get("findScene")
        if not isinstance(scene, dict):
            return None
        best_url: str | None = None
        best_area = -1
        for img in scene.get("images") or []:
            if not isinstance(img, dict) or not img.get("url"):
                continue
            try:
                w = int(img.get("width") or 0)
                h = int(img.get("height") or 0)
            except (TypeError, ValueError):
                w = h = 0
            area = w * h
            if area > best_area:
                best_area = area
                best_url = str(img["url"]).strip()
        return best_url or None

    def download_cover_to(
        self,
        image_url: str,
        dest_dir: Path,
        *,
        basename: str = ".stashdb-cover",
    ) -> Path | None:
        """Download cover image into dest_dir; returns path or None on failure."""
        url = (image_url or "").strip()
        if not url:
            return None
        dest_dir.mkdir(parents=True, exist_ok=True)
        try:
            with httpx.Client(timeout=30.0, follow_redirects=True) as client:
                resp = client.get(url)
                resp.raise_for_status()
                content = resp.content
                content_type = resp.headers.get("content-type")
        except httpx.HTTPError as exc:
            logger.warning("StashDB cover download failed: %s", exc)
            return None

        if len(content) > _MAX_COVER_BYTES:
            logger.warning(
                "StashDB cover too large (%d bytes > %d); skipping thumbnail",
                len(content),
                _MAX_COVER_BYTES,
            )
            return None

        ext = _extension_from_response(url, content_type)
        dest = dest_dir / f"{basename}{ext}"
        dest.write_bytes(content)
        return dest


def _flatten_fingerprint_matches(raw: Any) -> list[dict[str, Any]]:
    if not raw:
        return []
    if isinstance(raw, list) and raw and isinstance(raw[0], list):
        flat: list[dict[str, Any]] = []
        seen: set[str] = set()
        for sub in raw:
            for item in sub or []:
                if isinstance(item, dict) and item.get("id") not in seen:
                    seen.add(str(item.get("id")))
                    flat.append(item)
        return flat
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    return []


def _extension_from_response(url: str, content_type: str | None) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    mapping = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
    }
    if ct in mapping:
        return mapping[ct]
    path = urlparse(url).path
    suffix = Path(path).suffix.lower()
    if suffix in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
        return suffix if suffix != ".jpeg" else ".jpg"
    guessed = mimetypes.guess_extension(ct or "")
    if guessed:
        return ".jpg" if guessed == ".jpe" else guessed
    return ".jpg"


def resolve_stashdb_metadata(
    client: StashdbClient,
    oshash: str,
    job_dir: Path,
    *,
    existing_scene_id: str | None = None,
    existing_title: str | None = None,
    existing_cover_path: str | None = None,
) -> tuple[str | None, str | None, str | None]:
    """Lookup StashDB by OSHASH and cache cover on disk.

    Returns (scene_id, title, cover_path) — skips network if title already cached.
    """
    if existing_title and existing_cover_path and Path(existing_cover_path).is_file():
        return existing_scene_id, existing_title, existing_cover_path

    if not client.enabled:
        return None, None, None

    match: StashdbSceneMatch | None = None
    if existing_scene_id and existing_title:
        match = StashdbSceneMatch(scene_id=existing_scene_id, title=existing_title)
    else:
        try:
            match = client.find_by_oshash(oshash)
        except Exception as exc:
            logger.warning("StashDB OSHASH lookup failed: %s", exc)
            return None, None, None
        if not match:
            logger.info("StashDB: no match for OSHASH %s", oshash)
            return None, None, None
        logger.info("StashDB match: %r (%s)", match.title, match.scene_id)

    cover_path: str | None = existing_cover_path
    if existing_cover_path and Path(existing_cover_path).is_file():
        return match.scene_id, match.title, cover_path

    try:
        image_url = client.largest_image_url(match.scene_id)
    except Exception as exc:
        logger.warning("StashDB scene images query failed: %s", exc)
        image_url = None

    if not image_url:
        return match.scene_id, match.title, None

    downloaded = client.download_cover_to(image_url, job_dir)
    if downloaded:
        cover_path = str(downloaded)
    return match.scene_id, match.title, cover_path
