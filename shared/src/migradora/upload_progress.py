"""Structured upload progress for the WebUI (APU-style split + per-part bars)."""

from __future__ import annotations

from migradora.transfer_stats import format_size


class UploadProgressReporter:
    """Track split/upload progress for dashboard rendering."""

    def __init__(self, *, folder_name: str = "") -> None:
        self._folder_name = folder_name or "Root"
        self._split_mode = False
        self._source_bytes = 0
        self._part_count = 0
        self._parts: dict[int, dict] = {}
        self._current_part: int | None = None
        self._state: dict = {}
        self._status_text = ""

    @property
    def status_text(self) -> str:
        return self._status_text

    def set_activity(self, message: str) -> None:
        line = message.strip()
        if line:
            self._status_text = line

    def _commit(self, progress: dict, status_text: str) -> None:
        self._state = progress
        if status_text:
            self._status_text = status_text

    def set_splitting(self, *, source_bytes: int = 0, label: str = "") -> None:
        self._split_mode = True
        self._source_bytes = int(source_bytes or 0)
        msg = label or "Splitting video for Filester upload…"
        self._commit(
            {
                "type": "upload",
                "phase": "splitting",
                "mode": "split",
                "percent": 0,
                "source_bytes": self._source_bytes,
                "source_fmt": format_size(self._source_bytes) if self._source_bytes else "",
                "label": msg,
                "folder_name": self._folder_name,
                "parts": [],
            },
            msg,
        )

    def prepare_parts(self, part_count: int) -> None:
        count = max(0, int(part_count))
        self._part_count = count
        self._split_mode = count > 1
        for idx in range(1, count + 1):
            if idx not in self._parts:
                self._parts[idx] = {
                    "index": idx,
                    "label": f"Part {idx}",
                    "size_bytes": 0,
                    "percent": 0.0,
                    "status": "pending",
                }
        self._publish_splitting()

    def set_split_part_progress(
        self,
        part_index: int,
        *,
        label: str,
        done_bytes: int,
        total_bytes: int,
        part_count: int | None = None,
    ) -> None:
        idx = int(part_index or 0)
        if idx <= 0:
            return
        if part_count:
            self._part_count = int(part_count)
        rec = self._parts.setdefault(
            idx,
            {"index": idx, "label": label, "size_bytes": 0, "percent": 0.0, "status": "pending"},
        )
        rec["label"] = label or rec["label"]
        rec["size_bytes"] = int(total_bytes or rec.get("size_bytes") or 0)
        rec["status"] = "splitting"
        pct = (done_bytes / total_bytes * 100.0) if total_bytes > 0 else 0.0
        rec["percent"] = round(min(99.0, pct), 1)
        self._current_part = idx
        n = self._part_count or idx
        status = (
            f"Splitting part {idx}/{n}: {label} — {pct:.1f}% "
            f"({format_size(done_bytes)}/{format_size(total_bytes)})"
        )
        self._commit(
            {
                "type": "upload",
                "phase": "splitting",
                "mode": "split",
                "percent": round(self._overall_percent(), 1),
                "source_bytes": self._source_bytes,
                "source_fmt": format_size(self._source_bytes) if self._source_bytes else "",
                "label": status,
                "folder_name": self._folder_name,
                "part_index": idx,
                "part_count": n,
                "current_part": idx,
                "parts": self._parts_payload(),
            },
            status,
        )

    def register_part(
        self,
        part_index: int,
        label: str,
        size_bytes: int,
        part_count: int,
    ) -> None:
        idx = int(part_index or 0)
        if idx <= 0:
            return
        self._split_mode = True
        self._part_count = max(self._part_count, int(part_count or 0))
        rec = self._parts.setdefault(
            idx,
            {"index": idx, "label": label, "size_bytes": 0, "percent": 0.0, "status": "pending"},
        )
        rec["label"] = label or rec["label"]
        if size_bytes:
            rec["size_bytes"] = int(size_bytes)
        if rec.get("status") == "splitting":
            rec["status"] = "pending"
        self._publish_uploading(f"Ready to upload part {idx}/{self._part_count}: {label}")

    def complete_part(self, part_index: int) -> None:
        idx = int(part_index or 0)
        rec = self._parts.get(idx)
        if rec:
            rec["percent"] = 100.0
            rec["status"] = "done"
        if self._current_part == idx:
            self._current_part = None
        self._publish_uploading()

    def part_progress(
        self,
        part_index: int,
        done: int,
        total: int,
        *,
        speed_bps: float | None = None,
        eta_sec: float | None = None,
    ) -> None:
        idx = int(part_index or 0)
        rec = self._parts.get(idx)
        if not rec:
            return
        self._current_part = idx
        pct = (done / total * 100.0) if total > 0 else 0.0
        rec["percent"] = round(pct, 1)
        rec["status"] = "done" if pct >= 99.95 else "uploading"
        overall = self._overall_bytes()
        total_all = self._total_bytes()
        overall_pct = (overall / total_all * 100.0) if total_all > 0 else pct
        n = self._part_count or len(self._parts)
        speed = speed_bps or 0.0
        eta = int(eta_sec or 0)
        status = (
            f"Uploading part {idx}/{n}: {rec['label']} — {pct:.1f}% "
            f"({format_size(done)}/{format_size(total)}) "
            f"@ {format_size(speed)}/s — overall {overall_pct:.1f}%"
        )
        if eta > 0:
            status += f" — ETA {eta}s"
        self._commit(
            {
                "type": "upload",
                "phase": "uploading",
                "mode": "split",
                "percent": round(overall_pct, 1),
                "uploaded": overall,
                "total": total_all,
                "speed": speed,
                "eta": eta,
                "uploaded_fmt": format_size(overall),
                "total_fmt": format_size(total_all),
                "speed_fmt": f"{format_size(speed)}/s",
                "folder_name": self._folder_name,
                "part_index": idx,
                "part_count": n,
                "current_part": idx,
                "parts": self._parts_payload(),
            },
            status,
        )

    def single_progress(
        self,
        done: int,
        total: int,
        *,
        speed_bps: float | None = None,
        eta_sec: float | None = None,
    ) -> None:
        pct = (done / total * 100.0) if total > 0 else 0.0
        speed = speed_bps or 0.0
        eta = int(eta_sec or 0)
        status = (
            f"Uploading: {pct:.1f}% — {format_size(done)}/{format_size(total)} "
            f"@ {format_size(speed)}/s"
        )
        if eta > 0:
            status += f" — ETA {eta}s"
        self._commit(
            {
                "type": "upload",
                "phase": "uploading",
                "mode": "single",
                "percent": round(pct, 1),
                "uploaded": done,
                "total": total,
                "speed": speed,
                "eta": eta,
                "uploaded_fmt": format_size(done),
                "total_fmt": format_size(total),
                "speed_fmt": f"{format_size(speed)}/s",
                "folder_name": self._folder_name,
            },
            status,
        )

    def snapshot(self) -> dict:
        return dict(self._state)

    def _total_bytes(self) -> int:
        if self._source_bytes > 0:
            return self._source_bytes
        return sum(int(p.get("size_bytes") or 0) for p in self._parts.values())

    def _overall_bytes(self) -> int:
        total = 0.0
        for part in self._parts.values():
            size = int(part.get("size_bytes") or 0)
            pct = float(part.get("percent") or 0)
            status = part.get("status")
            if status == "done":
                total += size
            elif status in ("uploading", "splitting"):
                total += size * (pct / 100.0)
        return int(total)

    def _overall_percent(self) -> float:
        total_all = self._total_bytes()
        if total_all <= 0:
            return 0.0
        return self._overall_bytes() / total_all * 100.0

    def _parts_payload(self) -> list[dict]:
        out: list[dict] = []
        for idx in sorted(self._parts):
            part = self._parts[idx]
            out.append({
                "index": part["index"],
                "label": part["label"],
                "percent": part["percent"],
                "status": part["status"],
                "size_fmt": format_size(part.get("size_bytes") or 0),
            })
        if self._part_count > len(out):
            for idx in range(len(out) + 1, self._part_count + 1):
                out.append({
                    "index": idx,
                    "label": f"Part {idx}",
                    "percent": 0.0,
                    "status": "pending",
                    "size_fmt": "",
                })
        return out

    def _publish_splitting(self) -> None:
        n = self._part_count
        status = f"Splitting for Filester upload ({n} parts planned)" if n else "Splitting for Filester upload…"
        self._commit(
            {
                "type": "upload",
                "phase": "splitting",
                "mode": "split",
                "percent": round(self._overall_percent(), 1),
                "source_bytes": self._source_bytes,
                "source_fmt": format_size(self._source_bytes) if self._source_bytes else "",
                "label": status,
                "folder_name": self._folder_name,
                "part_count": n,
                "parts": self._parts_payload(),
            },
            status,
        )

    def _publish_uploading(self, status: str | None = None) -> None:
        overall = self._overall_bytes()
        total_all = self._total_bytes()
        overall_pct = (overall / total_all * 100.0) if total_all > 0 else 0.0
        n = self._part_count or len(self._parts)
        cur = self._current_part or 0
        if not status:
            status = f"Uploading split parts ({n} total) — overall {overall_pct:.1f}%"
            if cur:
                status = f"Uploading part {cur}/{n} — overall {overall_pct:.1f}%"
        self._commit(
            {
                "type": "upload",
                "phase": "uploading",
                "mode": "split",
                "percent": round(overall_pct, 1),
                "uploaded": overall,
                "total": total_all,
                "uploaded_fmt": format_size(overall),
                "total_fmt": format_size(total_all),
                "speed_fmt": "",
                "folder_name": self._folder_name,
                "part_count": n,
                "current_part": cur or None,
                "parts": self._parts_payload(),
            },
            status,
        )
