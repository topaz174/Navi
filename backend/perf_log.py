"""Per-goal latency logs: one folder per session, one text file per phase + 00_timeline.txt."""

from __future__ import annotations

import re
import time
import uuid
from datetime import datetime
from pathlib import Path

LOG_ROOT = Path(__file__).resolve().parent.parent / "logs"


class PerfSession:
    """Append-only latency trace. No API bodies, images, or prompts — only timings and labels."""

    def __init__(self, goal: str):
        self.goal = goal
        self.session_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
        slug = re.sub(r"[^\w\s-]", "", goal[:48]).strip().replace(" ", "_") or "goal"
        slug = slug[:48]
        self.dir = LOG_ROOT / f"navi_{self.session_id}_{slug}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._t0 = time.perf_counter()
        self._last = self._t0

        readme = self.dir / "00_README.txt"
        with open(readme, "w", encoding="utf-8") as f:
            f.write("Navi latency log — one folder per submitted goal.\n\n")
            f.write(f"session_id: {self.session_id}\n")
            f.write(f"goal: {goal}\n\n")
            f.write("Files:\n")
            f.write("  00_timeline.txt     — every event in chronological order\n")
            f.write("  01_planning.txt     — planning phase (capture + API)\n")
            f.write("  02_grounding.txt    — first CU grounding\n")
            f.write("  03_validation_*.txt — each validation after a user step\n")
            f.write("  05_waiting_*.txt    — diff monitor wait for screen change\n")
            f.write("  06_goal_check.txt   — Haiku goal check\n")
            f.write("\nEach line: wall-clock, ms since session start, ms since previous line, message.\n")

        self.event("00_timeline.txt", "SESSION_START")

    def event(self, filename: str, message: str, **kv: float | int | str | bool) -> None:
        now = time.perf_counter()
        wall = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        from_start_ms = (now - self._t0) * 1000
        delta_ms = (now - self._last) * 1000
        self._last = now
        line = f"{wall}  +{from_start_ms:9.1f}ms  Δ{delta_ms:8.1f}ms  {message}"
        if kv:
            line += "  " + " ".join(f"{k}={v}" for k, v in kv.items())
        line += "\n"
        path = self.dir / filename
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
        if filename != "00_timeline.txt":
            with open(self.dir / "00_timeline.txt", "a", encoding="utf-8") as f:
                f.write(f"{wall}  +{from_start_ms:9.1f}ms  Δ{delta_ms:8.1f}ms  [{filename}] {message}\n")
