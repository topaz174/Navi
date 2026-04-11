WS_PORT = 7373

CAPTURE_PULSE_MS = 1000
OVERLAY_HIDE_SETTLE_MS = 20

WS_EVENTS = {
    "dpr": "dpr",
    "step": "step",
    "loading": "loading",
    "confirm_done": "confirm_done",
    "done": "done",
    "error": "error",
    "goal": "goal",
    "user_confirmed_done": "user_confirmed_done",
    "user_continue": "user_continue",
    "cancel": "cancel",
    "hide": "hide",
    "show": "show",
    "voice_start": "voice_start",
    "voice_stop": "voice_stop",
    "voice_transcript": "voice_transcript",
    "voice_error": "voice_error",
}

# Screen diff monitor
POLL_INTERVAL_MS = 300
CLICK_POLL_INTERVAL_MS = 40
OVERLAY_SETTLE_MS = 250  # must be > step-in animation duration (180ms) so baseline captures final rendered state
# Fraction of all screen pixels that must visibly change before we validate.
DIFF_THRESHOLD = 0.0008
# Fraction of pixels in the focused target region that must visibly change before we validate.
LOCAL_DIFF_THRESHOLD = 0.01
# Further changed-pixel ratio between consecutive frames that counts as "still moving".
SETTLE_THRESHOLD = 0.0002
LOCAL_SETTLE_THRESHOLD = 0.003
SETTLE_WINDOW_MS = 500
COOLDOWN_MS = 0  # was 2000 — re-enable if diff monitor triggers prematurely after validation
IDLE_TIMEOUT_MS = 60000
CLICK_IDLE_TIMEOUT_MS = 60000
# Per-pixel RGB mean delta (0-255) required for a pixel to count as changed.
PIXEL_DIFF_MIN = 12
FOCUS_PADDING_PX = 96

API_RETRY_MAX = 3

# Models
PLANNING_MODEL = "claude-sonnet-4-6"
GOAL_CHECK_MODEL = "claude-haiku-4-5-20251001"
CU_BETA_FLAG = "computer-use-2025-11-24"

# Anthropic recommended max long edge for screenshots
SCREENSHOT_MAX_LONG_EDGE = 1100
SCREENSHOT_MAX_PIXELS = 800_000
