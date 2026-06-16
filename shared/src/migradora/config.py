"""Central configuration loaded from a single .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _env_int(key: str, default: int) -> int:
    val = _env(key)
    return int(val) if val else default


def _env_bool(key: str, default: bool = False) -> bool:
    val = _env(key).lower()
    if not val:
        return default
    return val in ("1", "true", "yes", "on")


def _env_list(key: str) -> list[str]:
    raw = _env(key)
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass
class Settings:
    # Gofile
    gofile_folder_urls: list[str] = field(default_factory=list)
    gofile_token: str = ""
    gofile_password: str = ""

    # Filester
    filester_api_key: str = ""
    filester_api_base: str = "https://u1.filester.me"
    filester_root_folder_name: str = "gofile-mirror"
    filester_max_file_bytes: int = 10_200_547_328  # 9.5 GiB

    # Paths
    download_dir: str = "/data/downloads"
    state_dir: str = "/data/state"
    log_dir: str = "/data/logs"
    db_path: str = "/data/state/queue.db"

    # Limits
    max_concurrent_files: int = 1
    min_free_disk_gb: int = 5
    verify_hash: bool = False
    stale_job_timeout_sec: int = 3600

    # Retries & throttling
    download_max_retries: int = 5
    download_retry_delay_sec: int = 30
    download_throttle_kbps: int = 0
    upload_max_retries: int = 5
    upload_retry_delay_sec: int = 30
    discovery_delay_sec: float = 2.0

    # Gofile traffic guard
    gofile_traffic_pause_gb: int = 90
    gofile_traffic_check_interval_sec: int = 3600

    # Filester storage guard
    filester_storage_pause_pct: float = 95.0

    # VPN
    vpn_enabled: bool = False
    pia_openvpn_user: str = ""
    pia_openvpn_password: str = ""
    pia_server_region: str = "Netherlands"
    vpn_rotate_on_ban: bool = True

    # Dashboard
    dashboard_host: str = "0.0.0.0"
    dashboard_port: int = 8080
    log_level: str = "INFO"

    # Worker
    worker_poll_interval_sec: float = 5.0
    heartbeat_interval_sec: int = 30

    @classmethod
    def load(cls, env_file: str | None = None) -> Settings:
        if env_file:
            load_dotenv(env_file, override=True)
        else:
            # Search upward from cwd and common locations
            for candidate in (".env", "/app/.env"):
                if Path(candidate).exists():
                    load_dotenv(candidate, override=True)
                    break
            else:
                load_dotenv(override=True)

        state_dir = _env("STATE_DIR", "/data/state")
        return cls(
            gofile_folder_urls=_env_list("GOFILE_FOLDER_URLS"),
            gofile_token=_env("GOFILE_TOKEN"),
            gofile_password=_env("GOFILE_PASSWORD"),
            filester_api_key=_env("FILESTER_API_KEY"),
            filester_api_base=_env("FILESTER_API_BASE", "https://u1.filester.me").rstrip("/"),
            filester_root_folder_name=_env("FILESTER_ROOT_FOLDER_NAME", "gofile-mirror"),
            filester_max_file_bytes=_env_int("FILESTER_MAX_FILE_BYTES", 10_200_547_328),
            download_dir=_env("DOWNLOAD_DIR", "/data/downloads"),
            state_dir=state_dir,
            log_dir=_env("LOG_DIR", "/data/logs"),
            db_path=_env("DB_PATH", f"{state_dir}/queue.db"),
            max_concurrent_files=_env_int("MAX_CONCURRENT_FILES", 1),
            min_free_disk_gb=_env_int("MIN_FREE_DISK_GB", 5),
            verify_hash=_env_bool("VERIFY_HASH", False),
            stale_job_timeout_sec=_env_int("STALE_JOB_TIMEOUT_SEC", 3600),
            download_max_retries=_env_int("DOWNLOAD_MAX_RETRIES", 5),
            download_retry_delay_sec=_env_int("DOWNLOAD_RETRY_DELAY_SEC", 30),
            download_throttle_kbps=_env_int("DOWNLOAD_THROTTLE_KBPS", 0),
            upload_max_retries=_env_int("UPLOAD_MAX_RETRIES", 5),
            upload_retry_delay_sec=_env_int("UPLOAD_RETRY_DELAY_SEC", 30),
            discovery_delay_sec=float(_env("DISCOVERY_DELAY_SEC", "2")),
            gofile_traffic_pause_gb=_env_int("GOFILE_TRAFFIC_PAUSE_GB", 90),
            gofile_traffic_check_interval_sec=_env_int("GOFILE_TRAFFIC_CHECK_INTERVAL_SEC", 3600),
            filester_storage_pause_pct=float(_env("FILESTER_STORAGE_PAUSE_PCT", "95")),
            vpn_enabled=_env_bool("VPN_ENABLED", False),
            pia_openvpn_user=_env("PIA_OPENVPN_USER"),
            pia_openvpn_password=_env("PIA_OPENVPN_PASSWORD"),
            pia_server_region=_env("PIA_SERVER_REGION", "Netherlands"),
            vpn_rotate_on_ban=_env_bool("VPN_ROTATE_ON_BAN", True),
            dashboard_host=_env("DASHBOARD_HOST", "0.0.0.0"),
            dashboard_port=_env_int("DASHBOARD_PORT", 8080),
            log_level=_env("LOG_LEVEL", "INFO"),
            worker_poll_interval_sec=float(_env("WORKER_POLL_INTERVAL_SEC", "5")),
            heartbeat_interval_sec=_env_int("HEARTBEAT_INTERVAL_SEC", 30),
        )

    def ensure_dirs(self) -> None:
        for path in (self.download_dir, self.state_dir, self.log_dir):
            Path(path).mkdir(parents=True, exist_ok=True)
