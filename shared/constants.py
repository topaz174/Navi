WS_PORT = 7373

CAPTURE_PULSE_MS = 1500
OVERLAY_HIDE_SETTLE_MS = 50  # propagation delay for setContentProtection before mss capture

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

# screen diff monitor
POLL_INTERVAL_MS = 300
CLICK_POLL_INTERVAL_MS = 40
OVERLAY_SETTLE_MS = 250
DIFF_THRESHOLD = 0.0008
LOCAL_DIFF_THRESHOLD = 0.01
SETTLE_THRESHOLD = 0.0002
LOCAL_SETTLE_THRESHOLD = 0.003
SETTLE_WINDOW_MS = 500
COOLDOWN_MS = 0
IDLE_TIMEOUT_MS = 60000
CLICK_IDLE_TIMEOUT_MS = 60000
PIXEL_DIFF_MIN = 12
FOCUS_PADDING_PX = 96
DIFF_SETTLE_FRAMES = 2  # consecutive stable frames before triggering validation

# fallback bounding-box sizes (api coordinate space) when the model omits a BOX
FALLBACK_BOX_TYPE = (220, 28)   # for type/key actions
FALLBACK_BOX_CLICK = (120, 24)  # for click actions

API_RETRY_MAX = 3

# models
PLANNING_MODEL = "claude-sonnet-4-6"
GOAL_CHECK_MODEL = "claude-haiku-4-5-20251001"
CU_BETA_FLAG = "computer-use-2025-11-24"

# anthropic image constraints
SCREENSHOT_MAX_LONG_EDGE = 1100
SCREENSHOT_MAX_PIXELS = 800_000

# voice recording
VOICE_SAMPLE_RATE = 16000
VOICE_CHUNK = 1024
VOICE_MAX_SECONDS = 8
