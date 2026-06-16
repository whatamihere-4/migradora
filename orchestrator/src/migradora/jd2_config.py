"""Ensure JD2 Deprecated API config exists after first-run initialization."""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

logger = logging.getLogger("migradora.jd2_config")

REMOTE_API_FILENAME = "org.jdownloader.api.RemoteAPIConfig.json"
GUI_SETTINGS_FILENAME = "org.jdownloader.settings.GraphicalUserInterfaceSettings.json"

DEFAULT_REMOTE_API = {
    "deprecatedapienabled": True,
    "deprecatedapilocalhostonly": False,
    "port": 3128,
}


def jd2_initialized(config_root: str | Path) -> bool:
    cfg = Path(config_root) / "cfg"
    return (cfg / GUI_SETTINGS_FILENAME).is_file()


def ensure_remote_api_enabled(
    config_root: str | Path,
    template_dir: str | Path = "/templates",
) -> bool:
    """
    Copy or patch RemoteAPI config once JD2 has completed first-run init.
    Returns True if config was created/updated (JD2 restart recommended).
    """
    config_root = Path(config_root)
    cfg = config_root / "cfg"
    api_path = cfg / REMOTE_API_FILENAME
    template_path = Path(template_dir) / REMOTE_API_FILENAME

    if not jd2_initialized(config_root):
        logger.warning(
            "JDownloader config not initialized yet — start jdownloader with an "
            "empty data/jd2/config volume first (do not pre-create cfg/)"
        )
        return False

    desired = DEFAULT_REMOTE_API.copy()
    if template_path.is_file():
        try:
            desired.update(json.loads(template_path.read_text()))
        except json.JSONDecodeError:
            pass

    changed = False
    if not api_path.is_file():
        cfg.mkdir(parents=True, exist_ok=True)
        api_path.write_text(json.dumps(desired, indent=2))
        logger.info("Created JD2 Remote API config at %s", api_path)
        changed = True
    else:
        try:
            current = json.loads(api_path.read_text())
        except json.JSONDecodeError:
            current = {}
        merged = {**current, **desired}
        if merged != current:
            api_path.write_text(json.dumps(merged, indent=2))
            logger.info("Updated JD2 Remote API config at %s", api_path)
            changed = True

    return changed
