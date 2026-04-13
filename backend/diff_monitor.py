import asyncio
import logging
import time

import numpy as np

from backend.perf_log import PerfSession
from shared.constants import (
    COOLDOWN_MS,
    DIFF_SETTLE_FRAMES,
    DIFF_THRESHOLD,
    FOCUS_PADDING_PX,
    IDLE_TIMEOUT_MS,
    LOCAL_DIFF_THRESHOLD,
    LOCAL_SETTLE_THRESHOLD,
    OVERLAY_SETTLE_MS,
    PIXEL_DIFF_MIN,
    POLL_INTERVAL_MS,
    SETTLE_THRESHOLD,
)

logger = logging.getLogger(__name__)


def compute_diff(frame_a: np.ndarray, frame_b: np.ndarray) -> float:
    """Fraction of pixels whose mean RGB delta exceeds PIXEL_DIFF_MIN."""
    pixel_delta = np.mean(
        np.abs(frame_a.astype(np.int16) - frame_b.astype(np.int16)),
        axis=2,
    )
    return float(np.mean(pixel_delta >= PIXEL_DIFF_MIN))


def _crop_focus_region(frame: np.ndarray, focus_rect: dict | None, logical_size: tuple[int, int] | None) -> np.ndarray | None:
    if not focus_rect or not logical_size:
        return None

    logical_w, logical_h = logical_size
    if not logical_w or not logical_h:
        return None

    scale_x = frame.shape[1] / logical_w
    scale_y = frame.shape[0] / logical_h
    left = max(0, int((focus_rect["left"] - FOCUS_PADDING_PX) * scale_x))
    top = max(0, int((focus_rect["top"] - FOCUS_PADDING_PX) * scale_y))
    right = min(frame.shape[1], int((focus_rect["left"] + focus_rect["width"] + FOCUS_PADDING_PX) * scale_x))
    bottom = min(frame.shape[0], int((focus_rect["top"] + focus_rect["height"] + FOCUS_PADDING_PX) * scale_y))

    if right <= left or bottom <= top:
        return None
    return frame[top:bottom, left:right]


class DiffMonitor:
    """Polls screenshots and fires when the screen changes and settles."""

    def __init__(
        self,
        capture_fn,
        on_settled,
        on_idle_timeout,
        ws_send,
        last_claude_call_time: float = 0.0,
        focus_rect: dict | None = None,
        logical_size: tuple[int, int] | None = None,
        perf_session: PerfSession | None = None,
        perf_log_file: str = "05_waiting.txt",
    ):
        self._capture_fn = capture_fn
        self._on_settled = on_settled
        self._on_idle_timeout = on_idle_timeout
        self._ws_send = ws_send
        self._running = False
        self._task: asyncio.Task | None = None
        self._last_claude_call_time = last_claude_call_time
        self._focus_rect = focus_rect
        self._logical_size = logical_size
        self._perf = perf_session
        self._perf_log_file = perf_log_file

    def record_claude_call(self):
        self._last_claude_call_time = time.time()

    def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.ensure_future(self._poll_loop())

    def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None

    async def _poll_loop(self):
        poll_sec = POLL_INTERVAL_MS / 1000.0
        idle_sec = IDLE_TIMEOUT_MS / 1000.0
        cooldown_sec = COOLDOWN_MS / 1000.0
        overlay_settle_sec = OVERLAY_SETTLE_MS / 1000.0

        # overlay stays below the diff threshold, so no need to hide for baseline
        if self._perf:
            self._perf.event(
                self._perf_log_file,
                "DiffMonitor poll_loop start",
                poll_ms=POLL_INTERVAL_MS,
                overlay_settle_ms=OVERLAY_SETTLE_MS,
                cooldown_ms=COOLDOWN_MS,
            )
        if overlay_settle_sec > 0:
            if self._perf:
                self._perf.event(self._perf_log_file, f"sleep overlay_settle {OVERLAY_SETTLE_MS}ms before baseline")
            await asyncio.sleep(overlay_settle_sec)
        if self._perf:
            self._perf.event(self._perf_log_file, "mss baseline capture (thread) start")
        t_cap0 = time.perf_counter()
        baseline = await asyncio.to_thread(self._capture_fn)
        if self._perf:
            self._perf.event(self._perf_log_file, "mss baseline capture done", ms=round((time.perf_counter() - t_cap0) * 1000, 1))
        baseline_focus = _crop_focus_region(baseline, self._focus_rect, self._logical_size)
        start_time = time.time()
        prev_frame = baseline
        prev_focus = baseline_focus
        settled_count = 0

        try:
            while self._running:
                await asyncio.sleep(poll_sec)
                if not self._running:
                    break

                try:
                    frame = await asyncio.to_thread(self._capture_fn)
                except Exception as e:
                    logger.warning("DiffMonitor capture failed: %s", e)
                    continue

                if frame.shape != baseline.shape:
                    logger.warning("DiffMonitor frame shape mismatch %s vs %s — re-baselining", frame.shape, baseline.shape)
                    baseline = frame
                    prev_frame = frame
                    continue

                diff_vs_baseline = compute_diff(baseline, frame)
                focus_frame = _crop_focus_region(frame, self._focus_rect, self._logical_size)
                diff_vs_focus_baseline = (
                    compute_diff(baseline_focus, focus_frame)
                    if baseline_focus is not None and focus_frame is not None and baseline_focus.shape == focus_frame.shape
                    else 0.0
                )
                logger.debug(
                    "DiffMonitor changed_px global=%.4f local=%.4f thresholds=(%.4f, %.4f)",
                    diff_vs_baseline,
                    diff_vs_focus_baseline,
                    DIFF_THRESHOLD,
                    LOCAL_DIFF_THRESHOLD,
                )

                if diff_vs_baseline > DIFF_THRESHOLD or diff_vs_focus_baseline > LOCAL_DIFF_THRESHOLD:
                    diff_vs_prev = compute_diff(prev_frame, frame)
                    diff_vs_focus_prev = (
                        compute_diff(prev_focus, focus_frame)
                        if prev_focus is not None and focus_frame is not None and prev_focus.shape == focus_frame.shape
                        else 0.0
                    )
                    if diff_vs_prev < SETTLE_THRESHOLD and diff_vs_focus_prev < LOCAL_SETTLE_THRESHOLD:
                        settled_count += 1
                    else:
                        settled_count = 0

                    if settled_count >= DIFF_SETTLE_FRAMES:
                        elapsed_since_claude = time.time() - self._last_claude_call_time
                        if self._perf:
                            self._perf.event(
                                self._perf_log_file,
                                "screen change settled (2 stable frames)",
                                global_diff=round(diff_vs_baseline, 4),
                                local_diff=round(diff_vs_focus_baseline, 4),
                            )
                        if elapsed_since_claude < cooldown_sec:
                            wait = cooldown_sec - elapsed_since_claude
                            if self._perf:
                                self._perf.event(self._perf_log_file, f"cooldown sleep {wait*1000:.0f}ms (since last Claude call)")
                            await asyncio.sleep(wait)

                        if self._running:
                            self._running = False
                            if self._perf:
                                self._perf.event(self._perf_log_file, "invoke on_settled → schedule VALIDATING")
                            await self._on_settled(frame)
                            return
                else:
                    settled_count = 0

                    # Check idle timeout
                    if time.time() - start_time > idle_sec:
                        if self._perf:
                            self._perf.event(self._perf_log_file, f"idle timeout ({IDLE_TIMEOUT_MS}ms) — nudge WS step")
                        await self._on_idle_timeout()
                        start_time = time.time()  # reset timer after nudge

                prev_frame = frame
                prev_focus = focus_frame
        except asyncio.CancelledError:
            pass
