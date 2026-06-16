"""Monitor Filester account storage usage."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from migradora.config import Settings
from migradora.models import QueueState
from migradora.queue.manager import QueueManager

logger = logging.getLogger("migradora.monitor.filester")


class FilesterStorageMonitor:
    def __init__(self, settings: Settings, queue: QueueManager) -> None:
        self.settings = settings
        self.queue = queue

    def fetch_storage_stats(self) -> dict[str, Any]:
        if not self.settings.filester_api_key:
            return {"error": "FILESTER_API_KEY not configured"}

        try:
            with httpx.Client(
                base_url=self.settings.filester_api_base,
                headers={"Authorization": f"Bearer {self.settings.filester_api_key}"},
                timeout=30,
            ) as client:
                resp = client.get("/api/v1/account")
                resp.raise_for_status()
                account = resp.json().get("data", resp.json())
                used = int(account.get("storage_used", 0))
                limit = int(account.get("storage_limit", 10 * 1024 ** 3))
                result = {
                    "storage_used_bytes": used,
                    "storage_limit_bytes": limit,
                    "storage_used_gb": round(used / (1024 ** 3), 2),
                    "storage_limit_gb": round(limit / (1024 ** 3), 2),
                    "files_count": account.get("files_count"),
                    "folders_count": account.get("folders_count"),
                    "api_requests_today": account.get("api_requests_today"),
                    "raw": account,
                }
                self.queue.log_bandwidth(
                    traffic_bytes=None,
                    storage_bytes=used,
                    source="filester",
                    raw=result,
                )
                return result
        except Exception as exc:
            logger.error("Failed to fetch Filester storage: %s", exc)
            return {"error": str(exc)}

    def check_and_pause(self) -> dict[str, Any]:
        stats = self.fetch_storage_stats()
        used = stats.get("storage_used_bytes")
        limit = stats.get("storage_limit_bytes")
        if used is not None and limit and limit > 0:
            pct = (used / limit) * 100
            stats["storage_used_pct"] = round(pct, 1)
            if pct >= self.settings.filester_storage_pause_pct:
                reason = (
                    f"Filester storage {pct:.1f}% full "
                    f"({stats.get('storage_used_gb')} / {stats.get('storage_limit_gb')} GB)"
                )
                logger.warning(reason)
                self.queue.set_queue_state(QueueState.PAUSED_STORAGE, reason)
                stats["paused"] = True
                stats["pause_reason"] = reason
            else:
                state, _ = self.queue.get_queue_state()
                if state == QueueState.PAUSED_STORAGE:
                    self.queue.set_queue_state(QueueState.RUNNING, "")
                    stats["resumed"] = True
        return stats
