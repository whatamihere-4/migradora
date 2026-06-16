"""Monitor Gofile account traffic usage."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from migradora.config import Settings
from migradora.models import QueueState
from migradora.queue.manager import QueueManager

logger = logging.getLogger("migradora.monitor.gofile")


class GofileTrafficMonitor:
    API_BASE = "https://api.gofile.io"

    def __init__(self, settings: Settings, queue: QueueManager) -> None:
        self.settings = settings
        self.queue = queue

    def fetch_account_stats(self) -> dict[str, Any]:
        token = self.settings.gofile_token
        if not token:
            return {"error": "GOFILE_TOKEN not configured", "manual_check": "https://gofile.io/myprofile"}

        headers = {"Authorization": f"Bearer {token}"}
        try:
            with httpx.Client(timeout=30) as client:
                id_resp = client.get(f"{self.API_BASE}/accounts/getid", headers=headers)
                id_data = id_resp.json()
                if id_data.get("status") != "ok":
                    return {
                        "error": id_data.get("status", "unknown"),
                        "manual_check": "https://gofile.io/myprofile",
                    }
                account_id = id_data["data"]["id"]
                acct_resp = client.get(
                    f"{self.API_BASE}/accounts/{account_id}",
                    headers=headers,
                )
                acct_data = acct_resp.json()
                if acct_data.get("status") != "ok":
                    return {
                        "error": acct_data.get("status", "unknown"),
                        "manual_check": "https://gofile.io/myprofile",
                    }
                stats = acct_data["data"].get("statsCurrent", {})
                traffic = stats.get("trafficWebDownloaded")
                storage = stats.get("storage")
                result = {
                    "account_id": account_id,
                    "traffic_downloaded_bytes": traffic,
                    "storage_used_bytes": storage,
                    "file_count": stats.get("fileCount"),
                    "folder_count": stats.get("folderCount"),
                    "tier": acct_data["data"].get("tier", "unknown"),
                    "raw": acct_data["data"],
                }
                self.queue.log_bandwidth(
                    traffic_bytes=traffic,
                    storage_bytes=storage,
                    source="gofile",
                    raw=result,
                )
                return result
        except Exception as exc:
            logger.error("Failed to fetch Gofile account stats: %s", exc)
            return {"error": str(exc), "manual_check": "https://gofile.io/myprofile"}

    def check_and_pause(self) -> dict[str, Any]:
        stats = self.fetch_account_stats()
        pause_gb = self.settings.gofile_traffic_pause_gb
        traffic = stats.get("traffic_downloaded_bytes")
        if traffic is not None:
            used_gb = traffic / (1024 ** 3)
            stats["traffic_used_gb"] = round(used_gb, 2)
            stats["traffic_limit_gb"] = 100
            if used_gb >= pause_gb:
                reason = f"Gofile traffic {used_gb:.1f} GB >= pause threshold {pause_gb} GB"
                logger.warning(reason)
                self.queue.set_queue_state(QueueState.PAUSED_TRAFFIC, reason)
                stats["paused"] = True
                stats["pause_reason"] = reason
            else:
                state, _ = self.queue.get_queue_state()
                if state == QueueState.PAUSED_TRAFFIC:
                    self.queue.set_queue_state(QueueState.RUNNING, "")
                    stats["resumed"] = True
        return stats
