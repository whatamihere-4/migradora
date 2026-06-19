"""Gluetun VPN helpers (PIA IP rotation for Gofile download blocks)."""

from __future__ import annotations

import logging
import time

import httpx

logger = logging.getLogger("migradora.vpn")

GOFILE_BLOCK_MARKERS = (
    "traffic",
    "bandwidth",
    "limit",
    "premium",
    "rate",
    "too many",
    "exceeded",
    "blocked",
    "ban",
    "quota",
)


def is_gofile_traffic_block(message: str) -> bool:
    text = message.lower()
    if "gofile" not in text and "api.gofile.io" not in text:
        return False
    if "429" in text and "guest account" in text:
        return False
    return any(marker in text for marker in GOFILE_BLOCK_MARKERS)


def get_egress_ip(control_url: str, timeout_sec: float = 10.0) -> str | None:
    """Return current public IP as seen through gluetun."""
    try:
        with httpx.Client(timeout=timeout_sec) as client:
            resp = client.get(f"{control_url.rstrip('/')}/v1/publicip/ip")
            resp.raise_for_status()
            data = resp.json()
            payload = data.get("public_ip") or data.get("data") or data
            if isinstance(payload, dict):
                return str(payload.get("public_ip") or payload.get("ip") or "")
            if isinstance(payload, str):
                return payload
    except Exception as exc:
        logger.debug("Could not read VPN egress IP: %s", exc)
    return None


def rotate_vpn(
    control_url: str = "http://gluetun:8000",
    *,
    wait_sec: float = 20.0,
    timeout_sec: float = 30.0,
) -> dict[str, str | None]:
    """
    Reconnect OpenVPN via gluetun control API to obtain a new egress IP.
    Returns before/after IPs when available.
    """
    base = control_url.rstrip("/")
    before = get_egress_ip(base, timeout_sec=timeout_sec)

    with httpx.Client(timeout=timeout_sec) as client:
        resp = client.put(f"{base}/v1/openvpn/status", json={"status": "stopped"})
        resp.raise_for_status()

    deadline = time.time() + wait_sec
    after: str | None = None
    while time.time() < deadline:
        time.sleep(2)
        after = get_egress_ip(base, timeout_sec=timeout_sec)
        if after and after != before:
            break

    logger.info("VPN rotated: %s -> %s", before or "?", after or "?")
    return {"ip_before": before, "ip_after": after}
