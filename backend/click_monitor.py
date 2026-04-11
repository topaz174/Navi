import asyncio
import ctypes
import ctypes.util
import logging
import time

from backend.perf_log import PerfSession
from shared.constants import CLICK_IDLE_TIMEOUT_MS, CLICK_POLL_INTERVAL_MS

logger = logging.getLogger(__name__)


class CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


_app_services_path = ctypes.util.find_library("ApplicationServices")
if not _app_services_path:
    _app_services_path = "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
_cg = ctypes.CDLL(_app_services_path)

_cg.CGEventCreate.restype = ctypes.c_void_p
_cg.CGEventCreate.argtypes = [ctypes.c_void_p]
_cg.CGEventGetLocation.restype = CGPoint
_cg.CGEventGetLocation.argtypes = [ctypes.c_void_p]
_cg.CGEventSourceButtonState.restype = ctypes.c_bool
_cg.CGEventSourceButtonState.argtypes = [ctypes.c_int, ctypes.c_int]
_cg.CFRelease.restype = None
_cg.CFRelease.argtypes = [ctypes.c_void_p]

kCGEventSourceStateCombinedSessionState = 0
kCGMouseButtonLeft = 0


def _left_button_down() -> bool:
    return bool(_cg.CGEventSourceButtonState(kCGEventSourceStateCombinedSessionState, kCGMouseButtonLeft))


def _mouse_location() -> tuple[float, float]:
    event = _cg.CGEventCreate(None)
    if not event:
        return (0.0, 0.0)
    try:
        point = _cg.CGEventGetLocation(event)
        return (float(point.x), float(point.y))
    finally:
        _cg.CFRelease(event)


class ClickMonitor:
    """Poll global left mouse button state and fire when a click lands inside the target rect."""

    def __init__(
        self,
        focus_rect: dict,
        on_click_inside,
        on_idle_timeout,
        perf_session: PerfSession | None = None,
        perf_log_file: str = "05_waiting_for_click.txt",
    ):
        self._focus_rect = focus_rect
        self._on_click_inside = on_click_inside
        self._on_idle_timeout = on_idle_timeout
        self._perf = perf_session
        self._perf_log_file = perf_log_file
        self._running = False
        self._task: asyncio.Task | None = None

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

    def _contains(self, x: float, y: float) -> bool:
        left = self._focus_rect["left"]
        top = self._focus_rect["top"]
        right = left + self._focus_rect["width"]
        bottom = top + self._focus_rect["height"]
        return left <= x <= right and top <= y <= bottom

    async def _poll_loop(self):
        poll_sec = CLICK_POLL_INTERVAL_MS / 1000.0
        idle_sec = CLICK_IDLE_TIMEOUT_MS / 1000.0
        last_down = _left_button_down()
        start_time = time.time()

        if self._perf:
            self._perf.event(
                self._perf_log_file,
                "ClickMonitor start",
                poll_ms=CLICK_POLL_INTERVAL_MS,
                idle_timeout_ms=CLICK_IDLE_TIMEOUT_MS,
                focus_left=round(self._focus_rect["left"], 1),
                focus_top=round(self._focus_rect["top"], 1),
                focus_width=round(self._focus_rect["width"], 1),
                focus_height=round(self._focus_rect["height"], 1),
            )

        try:
            while self._running:
                await asyncio.sleep(poll_sec)
                down = _left_button_down()

                # Detect button-up -> button-down edge as a click start.
                if down and not last_down:
                    x, y = _mouse_location()
                    inside = self._contains(x, y)
                    logger.debug("ClickMonitor click at (%.1f, %.1f) inside=%s", x, y, inside)
                    if inside:
                        if self._perf:
                            self._perf.event(
                                self._perf_log_file,
                                "click inside target",
                                click_x=round(x, 1),
                                click_y=round(y, 1),
                            )
                        self._running = False
                        await self._on_click_inside()
                        return
                    start_time = time.time()

                if time.time() - start_time > idle_sec:
                    if self._perf:
                        self._perf.event(self._perf_log_file, f"click idle timeout ({CLICK_IDLE_TIMEOUT_MS}ms) — nudge step")
                    await self._on_idle_timeout()
                    start_time = time.time()

                last_down = down
        except asyncio.CancelledError:
            pass
