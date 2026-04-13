# Navi

**AI overlay that shows you where to click instead of clicking for you.**

Navi is a macOS desktop app that takes a natural language goal — typed or spoken — reads your screen with Claude's Computer Use vision, and draws animated bounding boxes over the exact UI elements you need to interact with, step by step, until the task is done. You stay in control. Navi is the GPS, not the driver.

<table>
  <tr>
    <td><img src="https://github.com/user-attachments/assets/4a04685c-da58-4d13-aec0-4d3ca9e3bfa4" width="300" alt="Step 1"></td>
    <td><img src="https://github.com/user-attachments/assets/af50081b-1a14-487a-9b8e-b38cc2fef655" width="300" alt="Step 2"></td>
    <td><img src="https://github.com/user-attachments/assets/25dcc195-ea2e-4d62-aa70-e28cd6903b26" width="300" alt="Step 3"></td>
  </tr>
</table>
<img width="866" height="511" alt="image" src="https://github.com/user-attachments/assets/4a04685c-da58-4d13-aec0-4d3ca9e3bfa4" />
<img width="463" height="409" alt="image" src="https://github.com/user-attachments/assets/af50081b-1a14-487a-9b8e-b38cc2fef655" />
<img width="779" height="504" alt="image" src="https://github.com/user-attachments/assets/25dcc195-ea2e-4d62-aa70-e28cd6903b26" />


It works on any application visible on screen — mainstream, niche, modern, or legacy — to the best of the model's vision capability. No integrations, no browser extensions, no MCP servers. If it's on screen, Navi can guide you through it.

---

## Quick Start

**Prerequisites**
- macOS (only supported platform — see [Platform](#platform))
- Node.js 18+
- Python 3.12+
- An Anthropic API key (get one at [console.anthropic.com](https://console.anthropic.com))

**Setup**

```bash
git clone <repo>
cd Navi

# Python dependencies
pip install -r requirements.txt

# Node dependencies
npm install

# Create your local .env
cp .env.example .env
# Open .env and paste your ANTHROPIC_API_KEY
```

**Run**

```bash
npm run dev
```

This starts both the Python backend (WebSocket server on port 7373) and Electron frontend simultaneously via `concurrently`. Electron will retry its WebSocket connection until the Python server is ready.

On first launch, macOS may require **Screen Recording** permission for `mss` to capture screenshots. If the screen appears black, open **System Settings → Privacy & Security → Screen Recording** and enable Navi. The app should eventually prompt you to do this automatically.

---

## Platform

**macOS only, v0.** Single primary monitor. All coordinate math, DPR handling, screen capture, and Electron window management targets macOS. No cross-platform guards — no Windows paths.

The app lives in the macOS menu bar (not the Dock). Clicking **×** hides the overlay but keeps the backend running. The tray menu has **Show Navi** and **Quit**. `⌘Q` also quits cleanly.

---

## How It Works

### The Big Picture

Navi is built on Anthropic's [Computer Use](https://docs.anthropic.com/en/docs/build-with-claude/computer-use) beta API. The standard Computer Use loop is: screenshot → Claude returns a `tool_use` action (e.g. `left_click` at `[450, 320]`) → your app executes the action → screenshot again → repeat. Navi uses the same API but **intercepts the execution step**. Instead of calling `pyautogui.click()`, Navi renders a bounding box at the returned coordinates and tells the user to click it themselves.

On top of this interception pattern, Navi wraps the loop in a **plan-and-validate architecture**: one upfront call produces the full predicted step sequence, followed by validation calls after each user action. This separates cheap "did it work?" checks from expensive cold reasoning, and gives the user a visible plan from the start.

### State Machine

```
IDLE → PLANNING → DISPLAYING → WAITING_FOR_CHANGE → VALIDATING → DISPLAYING → ...
                                                           │
                                                     STATUS:complete
                                                           │
                                                      GOAL_CHECK
                                                           │
                                                         DONE
```

**IDLE** — User types or speaks a goal. Nothing else happens until submit.

**PLANNING** — A single `beta.messages.create` call (with the Computer Use tool active) takes the current screenshot and goal and returns *both* the full step plan *and* the bounding box for step 1 in a single response. This saves one full API round-trip vs the earlier sequential plan-then-ground flow. The model is instructed to emit `PLAN:{...}`, `USER_INSTRUCTION:`, and `BOX:{...}` in its text alongside a `left_click` tool call pointing at step 1's element.

**DISPLAYING** — The Python engine sends a `step` WebSocket event to Electron with the instruction, box coordinates, and step number. Electron renders the bounding box and tooltip. Screen diff polling begins immediately.

**WAITING_FOR_CHANGE** — A background asyncio task (`DiffMonitor`) polls screenshots at 300ms intervals. It computes pixel diffs both globally (whole screen) and locally (cropped region around the current bounding box with 96px padding). Three conditions must be met before firing validation:
1. Global changed-pixel fraction exceeds `DIFF_THRESHOLD` (0.08%) OR local fraction exceeds `LOCAL_DIFF_THRESHOLD` (1%)
2. The diff has "settled": two consecutive polls show < `SETTLE_THRESHOLD` / `LOCAL_SETTLE_THRESHOLD` further change — UI animations have finished
3. Baseline capture happens 250ms after the step is displayed (`OVERLAY_SETTLE_MS`) so the bounding box's own `step-in` animation (180ms) has finished painting before we start watching

**Auto-advance without validation:** if the current step is `click` and the next planned step is `type` or `key`, Navi auto-advances when the screen changes — no validation call needed. The keyboard action targets the same element we just clicked into.

**VALIDATING** — Hides the overlay, captures a fresh screenshot, shows the overlay again, then calls `beta.messages.create` with the current screenshot + goal + full plan + current step context. The model returns where the user should act next (coordinates + BOX) and a `STATUS` tag:
- `confirmed` — step completed, advance to next planned step
- `retry` — step didn't complete, re-show same step
- `replan` — unexpected screen state; model's tool_use and instruction replace the remaining plan
- `complete` — task is fully done, proceed to goal check

**GOAL_CHECK** — An isolated `messages.create` call using `claude-haiku-4-5` (no tools, no history). Fresh screenshot + original goal only. The lightweight judge checks: does the current screen reflect a completed goal? Returns `ACHIEVED` or `NOT_ACHIEVED: <reason>`. If not achieved, its reasoning is injected into the validation context and validation resumes. If achieved, Electron shows a "Looks right?" confirmation. User can say Yes (done) or Keep going (resumes validation).

**DONE** — Completion animation. Session ends. New goal input is available.

---

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                     USER'S DESKTOP                        │
│  ┌─────────────────────────────────────────────────────┐  │
│  │  Target App (any — CapCut, AWS, Blender, etc.)      │  │
│  └─────────────────────────────────────────────────────┘  │
│  ┌─────────────────────────────────────────────────────┐  │
│  │  NAVI OVERLAY (transparent, always-on-top)          │  │
│  │  - Bounding box at target coordinates               │  │
│  │  - Instruction tooltip                              │  │
│  │  - Step counter / progress HUD                      │  │
│  │  - Input bar (text + mic toggle)                    │  │
│  └─────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────┘
          │                          ▲
          │ screenshot               │ render overlay
          ▼                          │
┌──────────────────────────────────────────────────────────┐
│                  NAVI CORE ENGINE (Python)                 │
│                                                           │
│  Screenshot ──► scale ──► Anthropic CU API               │
│                                │                          │
│                                ▼                          │
│                    tool_use (coordinate) + text           │
│                    (USER_INSTRUCTION, BOX, STATUS)        │
│                                │                          │
│            intercept ──► send step WS event               │
│                         → render bounding box             │
└──────────────────────────────────────────────────────────┘
```

### Process Layout

```
npm run dev
  ├─ electron .            → main.js → BrowserWindow → renderer/
  └─ python backend/main.py → asyncio WebSocket server on :7373
                              → NaviEngine (state machine)
                              → DiffMonitor (background polling)
                              → api.py (Anthropic calls)
```

---

## Codebase Map

```
Navi/
├── main.js                  Electron main process — window creation, tray, IPC
├── preload.js               Context bridge (contextIsolation=true)
├── package.json
├── requirements.txt
├── .env                     ANTHROPIC_API_KEY (local, not committed)
├── .env.example             Template for .env
│
├── renderer/
│   ├── index.html
│   ├── app.js               All UI logic — WebSocket client, DOM updates, animations
│   ├── styles.css           All styles — animations defined here (step-in = 180ms)
│   └── constants.js         All design tokens and WS event names for the renderer
│
├── backend/
│   ├── main.py              asyncio entry point — starts WS server, routes events
│   ├── engine.py            NaviEngine — the state machine
│   ├── api.py               All Anthropic API calls (planning, validation, goal check)
│   ├── diff_monitor.py      DiffMonitor — background screen change detection
│   ├── screenshot.py        mss capture, scale, encode, overlay-hide helper
│   ├── scaling.py           get_scale_factor() — keeps images within Anthropic limits
│   └── perf_log.py          PerfSession — per-goal latency logs to logs/
│
└── shared/
    └── constants.py         All tuning constants and WS event names for Python
```

---

## Key Modules

### `backend/engine.py` — NaviEngine

The state machine. An instance is created per WebSocket connection. Handles `goal`, `cancel`, `user_confirmed_done`, and `user_continue` events. Drives the PLANNING → DISPLAYING → WAITING_FOR_CHANGE → VALIDATING → GOAL_CHECK loop.

Key methods:
- `handle_goal(goal)` — resets state, starts the pipeline
- `_do_planning()` — calls `planning_and_grounding_call`, stores plan and coordinates
- `_do_validating()` — calls `validation_call`, reads STATUS, transitions accordingly
- `_apply_result(result)` — converts API-space coordinates to logical CSS pixels
- `_maybe_auto_advance_without_validation()` — skips CU call for click→type/key sequences

### `backend/api.py` — API calls

Four functions, each with a retry loop (up to 5 iterations inside the loop, plus `API_RETRY_MAX` outer retries for the planning call):

**`planning_and_grounding_call(b64, goal, scaled_w, scaled_h, dpr, perf)`**
The merged first call. Uses `beta.messages.create` with the CU tool. Iterates until it gets a plan + click coordinates + an explicit `BOX:{...}` rectangle. If the model returns coords but no BOX, it sends back a `tool_result` with a "Navi intercepted" message and asks for just the BOX — no new screenshot needed.

**`grounding_call(b64, goal, plan, step_1_instruction, ...)`**
Standalone grounding (not used in the main flow — `_do_planning` calls `planning_and_grounding_call` instead). Kept for debugging or alternative entry points.

**`validation_call(b64, goal, plan, step_n, step_instruction, messages, ...)`**
Per-step validation. Passes the settled screenshot + goal + plan + step context. Currently called with `messages=[]` (no history) — the history plumbing exists but is disabled; see the `TODO` comment in `_do_validating`.

**`goal_check_call(b64, goal, perf)`**
Isolated Haiku call. No tools, no history. Returns `{"achieved": bool, "reasoning": str}`.

**`_parse_cu_response(response)`**
Parses every CU response into a normalized dict: `{x, y, box_x, box_y, w, h, instruction, status, action_type, raw_text}`. Extracts `BOX:{...}` via a brace-depth parser, `STATUS:` via regex, and `USER_INSTRUCTION:` via regex. The `BOX` field (all four corners + dimensions) is preferred over `BOUNDS` (dimensions only).

### `backend/diff_monitor.py` — DiffMonitor

Asyncio task that polls screenshots using `mss` and detects when the screen has changed *and settled*. Two diff signals:
- **Global diff** — fraction of all screen pixels whose mean RGB delta exceeds `PIXEL_DIFF_MIN` (12) compared to baseline
- **Local diff** — same computation on a crop around the current bounding box (`FOCUS_PADDING_PX = 96px` margin), scaled from logical CSS pixels to physical capture pixels

Either signal exceeding its threshold starts the settle countdown. Two consecutive polls below the settle thresholds triggers validation. A 250ms settle wait at the start (`OVERLAY_SETTLE_MS`) ensures the bounding box animation has finished rendering before the baseline is captured — without this, the animation itself would look like a screen change and trigger false validation.

### `backend/screenshot.py` — Capture

- `capture_screenshot()` — `mss.monitors[1]`, BGRA→RGB, PNG bytes
- `capture_and_encode(dpr)` — captures, scales via `get_scale_factor`, base64-encodes
- `capture_raw_array()` — returns a numpy RGB array (for diff monitor — no encoding overhead)
- `capture_with_overlay_hidden(ws_send, dpr)` — sends `hide` WS event, sleeps 20ms, captures, sends `show`

### `backend/scaling.py` — Coordinate Scaling

Anthropic constrains images to a max long edge (currently set to 1100px in `shared/constants.py`) and a max pixel count (800,000). `get_scale_factor` picks the most restrictive constraint:

```python
scale = min(1.0,
    SCREENSHOT_MAX_LONG_EDGE / max(width, height),
    math.sqrt(SCREENSHOT_MAX_PIXELS / (width * height))
)
```

On Retina Macs (DPR = 2), `mss` captures at physical resolution (2× logical). `_apply_result` in `engine.py` converts API-space coordinates back to logical CSS pixels using:

```python
ratio_x = logical_w / scaled_w
ratio_y = logical_h / scaled_h
css_x = model_x * ratio_x
```

Electron sends `logical_w`, `logical_h`, and `work_area_y` (menu bar + notch height) on connect via the `dpr` WS event.

### `backend/perf_log.py` — Performance Logging

Every goal session creates a folder under `logs/` named `navi_<timestamp>_<uuid>_<goal_slug>/`. Each phase writes to its own file and also appends to `00_timeline.txt`. Lines include wall clock, ms since session start, and ms since previous event. `logs/` is in `.gitignore`.

---

## WebSocket Protocol

Python runs the server on `localhost:7373`. Electron connects on startup with exponential backoff. All event names are defined in `shared/constants.py` (Python) and `renderer/constants.js` (Electron) — no string literals for event names anywhere in the codebase.

| Event | Direction | Payload |
|-------|-----------|---------|
| `dpr` | Electron → Python | `{ scaleFactor, logicalWidth, logicalHeight, workAreaY }` — sent once on connect |
| `goal` | Electron → Python | `{ text: string }` |
| `cancel` | Electron → Python | `{}` |
| `user_confirmed_done` | Electron → Python | `{}` |
| `user_continue` | Electron → Python | `{}` |
| `step` | Python → Electron | `{ instruction, x, y, left, top, w, h, action_type, step_num, total_steps }` |
| `loading` | Python → Electron | `{ active: bool }` |
| `confirm_done` | Python → Electron | `{ reasoning: string }` |
| `done` | Python → Electron | `{}` |
| `error` | Python → Electron | `{ message: string }` |
| `hide` | Python → Electron | `{}` — hide overlay before screenshot |
| `show` | Python → Electron | `{}` — restore overlay after screenshot |

The `step` event carries both `left/top` (BOX top-left corner, preferred) and `x/y` (click-point center, fallback). The renderer uses `left/top` to position the box when present.

---

## Models

| Call | Model | Notes |
|------|-------|-------|
| Planning + grounding | `claude-sonnet-4-6` | With `computer-use-2025-11-24` beta header |
| Validation | `claude-sonnet-4-6` | Same |
| Goal check | `claude-haiku-4-5-20251001` | No tools, no history — binary judge |

Model IDs are defined in `shared/constants.py`. Verify against [Anthropic's models page](https://docs.anthropic.com/en/docs/about-claude/models) before running — IDs change with releases.

---

## Design System

All design tokens live in `renderer/constants.js`. No color, opacity, timing, or spacing value appears anywhere else in the renderer codebase.

```javascript
COLORS = {
  accent:       '#3B82F6',   // electric blue — bounding box, active states
  accentDim:    '#3B82F640', // 25% opacity — loading states, subtle glows
  surface:      '#0A0A0ABA', // near-black at ~73% opacity — HUD panels
  surfaceHover: '#1A1A1AB0',
  text:         '#F0F0F0',
  textMuted:    '#A0A0A0',
  border:       '#3B82F620',
}

ANIMATION = {
  glowPulseDuration:    '2s',
  stepInDuration:       '180ms',  // bounding box scale-in
  loadingCycleDuration: '1.4s',
}
```

The overlay window uses `setAlwaysOnTop(true, 'screen-saver')` (macOS `NSScreenSaverWindowLevel`) so it stays above full-screen apps. Click-through (`setIgnoreMouseEvents`) is toggled via Electron-internal IPC on `mouseenter`/`mouseleave` for interactive HUD elements — Python has no involvement in this.

---

## Tuning Constants

All live in `shared/constants.py`:

| Constant | Value | What it controls |
|----------|-------|-----------------|
| `OVERLAY_SETTLE_MS` | 250ms | Wait after displaying a step before capturing diff baseline. Must exceed `stepInDuration` (180ms) or the animation itself triggers false validation. |
| `POLL_INTERVAL_MS` | 300ms | How often DiffMonitor checks for screen changes |
| `DIFF_THRESHOLD` | 0.0008 | Global: 0.08% of all pixels must change |
| `LOCAL_DIFF_THRESHOLD` | 0.01 | Local: 1% of target region pixels must change |
| `PIXEL_DIFF_MIN` | 12 | Per-pixel RGB mean delta to count as "changed" |
| `SETTLE_THRESHOLD` | 0.0002 | Global: frame-to-frame stability threshold |
| `LOCAL_SETTLE_THRESHOLD` | 0.003 | Local: frame-to-frame stability in target region |
| `FOCUS_PADDING_PX` | 96 | Padding around bounding box for local diff crop |
| `COOLDOWN_MS` | 0 | Min ms between Claude calls (disabled) |
| `IDLE_TIMEOUT_MS` | 60000 | Nudge user after 60s of no screen change |
| `SCREENSHOT_MAX_LONG_EDGE` | 1100 | Anthropic image size limit (long edge, px) |
| `SCREENSHOT_MAX_PIXELS` | 800,000 | Anthropic image size limit (total pixels) |

---

## Latency Profile

From real session logs (single-monitor Retina Mac, Claude Sonnet 4.6):

| Phase | Typical duration |
|-------|-----------------|
| Overlay hide + 20ms sleep | ~20ms |
| Screenshot capture + encode | ~70–110ms |
| `planning_and_grounding_call` HTTP response | ~5–10s |
| `validation_call` HTTP response | ~2–7s |
| `goal_check_call` (Haiku) | ~1.5–2s |
| Anthropic rate-limit sleep | 15s flat |

The dominant latency source is the API call itself. Rate-limit backoff (HTTP 429) adds 15s per hit; heavy usage sessions can accumulate multiple hits per step. Local capture and encoding is not a meaningful bottleneck (~0.1s order of magnitude).

---

## Engineering Conventions

- **No magic numbers or string literals.** All tuning values go in `shared/constants.py`. All event names are imported from constants in both Python and JS.
- **One source of truth per concern.** Python owns screenshot capture, API calls, and screen monitoring. Electron owns visual output. They communicate only via the WebSocket protocol above.
- **No fallback hallucinations.** If the model returns coordinates without a `BOX`, Navi does a follow-up turn to request the rectangle explicitly rather than using a hardcoded fallback size. Fallback dimensions (`120×24` for click, `220×28` for type/key) are a last resort logged visibly.
- **Perf-log everything.** Every phase emits timestamped events via `PerfSession`. When something feels slow, check `logs/<session>/00_timeline.txt` before guessing.
- **Comments explain intent, not mechanics.** No "# increment counter" comments. TODOs belong in code with a reason.

---

## Tech Stack

| Layer | Technology |
|-------|------------|
| Screenshot capture | `mss` ~30ms, `monitors[1]` |
| Image processing | `Pillow` — resize/encode for API |
| AI backbone | Claude Sonnet 4.6 + Haiku 4.5 |
| Computer Use tool | `computer_20251124` beta |
| Overlay shell | Electron (transparent, `screen-saver` level) |
| IPC | WebSocket `localhost:7373` |
| Screen diff | `mss` + `numpy` |
| Voice input | Web Speech API (Chromium-native in Electron) |
| Animations | CSS keyframes (all values from constants) |
| Dev runner | `concurrently` |

---

## Known Issues / Open TODOs

- **Validation history disabled.** `_do_validating` calls `validation_call` with `messages=[]` — the history plumbing exists but is bypassed. Re-enabling it would give the model better context across steps but increases token usage and rate-limit pressure. See the comment in `engine.py`.
- **Rate limits.** On the default Anthropic tier, sustained use hits 429s frequently. Each hit costs 15s. Upgrade your tier or add jitter/exponential backoff beyond the current flat 15s.
- **Single monitor only.** `mss.monitors[1]` is the primary display. Multi-monitor support is out of scope for v0.
- **`package.json` dev script uses an absolute Python path.** Update the `dev` script to use `python3` (or your venv's python) before sharing with others.
