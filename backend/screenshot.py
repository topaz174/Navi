import asyncio
import base64
import io
import logging
import time

import mss
import mss.tools
from PIL import Image

from backend.scaling import get_scale_factor

logger = logging.getLogger(__name__)


def capture_screenshot() -> tuple[bytes, int, int]:
    """Capture the primary monitor and return (png_bytes, width, height)."""
    with mss.mss() as sct:
        monitor = sct.monitors[1]
        raw = sct.grab(monitor)
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    width, height = img.size
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue(), width, height


def capture_and_encode(dpr: float = 2.0) -> tuple[str, int, int, float]:
    """Capture, scale for the API, and return (base64_png, scaled_w, scaled_h, scale_factor).

    The returned dimensions are in API coordinate space (what Claude sees).
    """
    png_bytes, phys_w, phys_h = capture_screenshot()
    scale = get_scale_factor(phys_w, phys_h)

    if scale < 1.0:
        img = Image.open(io.BytesIO(png_bytes))
        new_w = int(phys_w * scale)
        new_h = int(phys_h * scale)
        img = img.resize((new_w, new_h), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png_bytes = buf.getvalue()
        scaled_w, scaled_h = new_w, new_h
    else:
        scaled_w, scaled_h = phys_w, phys_h

    b64 = base64.standard_b64encode(png_bytes).decode("ascii")
    return b64, scaled_w, scaled_h, scale


def capture_raw_array():
    """Capture primary monitor and return a numpy-compatible RGB array for diffing."""
    import numpy as np

    with mss.mss() as sct:
        monitor = sct.monitors[1]
        raw = sct.grab(monitor)
        # mss returns BGRA; convert to RGB via numpy slicing
        arr = np.frombuffer(raw.bgra, dtype=np.uint8).reshape(raw.height, raw.width, 4)
        return arr[:, :, :3].copy()  # drop alpha, ensure contiguous


async def capture_with_overlay_hidden(
    ws_send,
    dpr: float = 2.0,
    perf=None,
    phase_file: str = "capture.txt",
) -> tuple[str, int, int, float]:
    """Hide overlay, capture screenshot, show overlay again."""
    from shared.constants import WS_EVENTS

    if perf:
        perf.event(phase_file, "WS hide overlay (Electron)")
    await ws_send(WS_EVENTS["hide"], {})
    try:
        if perf:
            perf.event(phase_file, "asyncio.sleep 20ms (overlay paint)")
        await asyncio.sleep(0.020)  # wait ~1 frame for overlay to hide
        if perf:
            perf.event(phase_file, "capture_and_encode (thread pool) start")
        t0 = time.perf_counter()
        out = await asyncio.to_thread(capture_and_encode, dpr)
        if perf:
            perf.event(phase_file, "capture_and_encode done", ms=round((time.perf_counter() - t0) * 1000, 1))
        return out
    finally:
        await ws_send(WS_EVENTS["show"], {})
        if perf:
            perf.event(phase_file, "WS show overlay (Electron)")
