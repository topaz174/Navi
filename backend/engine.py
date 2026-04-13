import asyncio
import logging
import time

from backend.api import (
    goal_check_call,
    grounding_call,
    planning_and_grounding_call,
    planning_call,
    validation_call,
)
from backend.click_monitor import ClickMonitor
from backend.diff_monitor import DiffMonitor
from backend.perf_log import PerfSession
from backend.screenshot import capture_raw_array, capture_with_overlay_hidden
from shared.constants import WS_EVENTS

logger = logging.getLogger(__name__)

# States
IDLE = "IDLE"
PLANNING = "PLANNING"
GROUNDING = "GROUNDING"
DISPLAYING = "DISPLAYING"
WAITING_FOR_CHANGE = "WAITING_FOR_CHANGE"
VALIDATING = "VALIDATING"
GOAL_CHECK = "GOAL_CHECK"
DONE = "DONE"

CLICK_ACTIONS = {"click", "left_click", "right_click", "middle_click", "double_click"}

_MAX_REPLANS = 3  # cap on how many times the engine may re-plan after a NOT_ACHIEVED goal check


class NaviEngine:
    def __init__(self, ws_send):
        self._ws_send = ws_send
        self._state = IDLE
        self._goal = ""
        self._plan = None
        self._steps = []
        self._current_step = 0
        self._total_steps = 0
        self._dpr = 2.0
        self._logical_w = 0
        self._logical_h = 0
        self._work_area_y = 0
        self._scale_factor = 1.0
        self._scaled_w = 0
        self._scaled_h = 0
        self._messages: list = []
        self._diff_monitor: DiffMonitor | None = None
        self._click_monitor: ClickMonitor | None = None
        self._cancelled = False
        self._last_claude_call_time = 0.0
        self._replan_count = 0
        self._perf: PerfSession | None = None

    def set_dpr(self, dpr: float, logical_w: int = 0, logical_h: int = 0, work_area_y: int = 0):
        self._dpr = dpr
        self._logical_w = logical_w
        self._logical_h = logical_h
        self._work_area_y = work_area_y  # menu bar + notch height in logical px
        logger.info("DPR=%s logical=%dx%d workAreaY=%d (notch+menubar offset)",
                    dpr, logical_w, logical_h, work_area_y)

    async def handle_goal(self, goal: str):
        """User submitted a goal — start the pipeline."""
        self._goal = goal
        self._plan = None
        self._steps = []
        self._current_step = 0
        self._messages = []
        self._cancelled = False
        self._last_claude_call_time = 0.0
        self._replan_count = 0
        self._perf = PerfSession(goal)

        await self._transition(PLANNING)

    async def handle_cancel(self):
        self._cancelled = True
        if self._diff_monitor:
            self._diff_monitor.stop()
        if self._click_monitor:
            self._click_monitor.stop()
        self._state = IDLE

    async def handle_user_confirmed_done(self):
        await self._ws_send(WS_EVENTS["done"], {})
        self._state = DONE

    async def handle_user_continue(self):
        """User says task isn't done — resume validation."""
        await self._transition(VALIDATING)

    async def _transition(self, new_state: str):
        if self._cancelled:
            return
        self._state = new_state
        logger.info("State -> %s", new_state)

        try:
            if new_state == PLANNING:
                await self._do_planning()
            elif new_state == GROUNDING:
                await self._do_grounding()
            elif new_state == DISPLAYING:
                await self._do_displaying()
            elif new_state == WAITING_FOR_CHANGE:
                self._do_waiting()
            elif new_state == VALIDATING:
                await self._do_validating()
            elif new_state == GOAL_CHECK:
                await self._do_goal_check()
        except Exception as e:
            logger.exception("Error in state %s", new_state)
            if self._perf:
                self._perf.event("00_timeline.txt", f"EXCEPTION in {new_state}: {e!r}")
            await self._ws_send(WS_EVENTS["show"], {})
            await self._ws_send(WS_EVENTS["error"], {"message": str(e)})
            await self._ws_send(WS_EVENTS["loading"], {"active": False})
            self._state = IDLE

    async def _do_planning(self):
        """Plan and ground step 1 in a single CU call — saves one full API round-trip."""
        await self._ws_send(WS_EVENTS["loading"], {"active": True})
        if self._perf:
            self._perf.event("01_planning_and_grounding.txt", "state _do_planning: WS loading=true")

        b64, sw, sh, scale = await capture_with_overlay_hidden(
            self._ws_send, self._dpr, perf=self._perf, phase_file="01_planning_and_grounding.txt",
        )
        self._scale_factor = scale
        self._scaled_w = sw
        self._scaled_h = sh

        if self._perf:
            self._perf.event("01_planning_and_grounding.txt", "asyncio.to_thread(planning_and_grounding_call) start")
        plan, result = await asyncio.to_thread(
            planning_and_grounding_call,
            b64,
            self._goal,
            sw,
            sh,
            self._dpr,
            self._perf,
        )
        self._last_claude_call_time = time.time()

        self._plan = plan
        self._steps = plan.get("steps", [])
        self._total_steps = len(self._steps)

        if not self._steps:
            await self._ws_send(WS_EVENTS["error"], {"message": "No steps in plan"})
            self._state = IDLE
            return

        self._current_step = 1
        self._apply_result(result)
        self._display_instruction = self._steps[0]["instruction"]

        self._messages.append({
            "role": "assistant",
            "content": [{"type": "text", "text": result.get("raw_text", "")}],
        })

        logger.info("Plan+ground done: %d steps", self._total_steps)
        if self._perf:
            self._perf.event("01_planning_and_grounding.txt", f"done steps={self._total_steps}")
        await self._ws_send(WS_EVENTS["loading"], {"active": False})
        await self._transition(DISPLAYING)

    async def _do_grounding(self):
        """Standalone grounding — not used in the main flow (planning now merges this).
        Kept for potential direct calls or debugging."""
        await self._ws_send(WS_EVENTS["loading"], {"active": True})

        if not self._steps:
            await self._ws_send(WS_EVENTS["error"], {"message": "No steps in plan"})
            self._state = IDLE
            return

        step_1 = self._steps[0]
        b64, sw, sh, scale = await capture_with_overlay_hidden(self._ws_send, self._dpr)
        self._scale_factor = scale
        self._scaled_w = sw
        self._scaled_h = sh

        result = await asyncio.to_thread(
            grounding_call, b64, self._goal, self._plan, step_1["instruction"], sw, sh, self._dpr,
        )
        self._last_claude_call_time = time.time()
        self._current_step = 1
        self._apply_result(result)
        self._display_instruction = step_1["instruction"]
        self._messages.append({
            "role": "assistant",
            "content": [{"type": "text", "text": result.get("raw_text", "")}],
        })
        await self._ws_send(WS_EVENTS["loading"], {"active": False})
        await self._transition(DISPLAYING)

    def _replace_remaining_plan(self, remaining_plan: dict | None) -> None:
        if not remaining_plan:
            return
        raw_steps = remaining_plan.get("steps", [])
        if not isinstance(raw_steps, list) or not raw_steps:
            return

        prefix = list(self._steps[:self._current_step])
        normalized_suffix = []
        for offset, step in enumerate(raw_steps, start=1):
            if not isinstance(step, dict):
                continue
            normalized = dict(step)
            normalized["n"] = self._current_step + offset
            normalized_suffix.append(normalized)

        if not normalized_suffix:
            return

        self._steps = prefix + normalized_suffix
        self._plan = {"steps": self._steps}
        self._total_steps = len(self._steps)
        if self._perf:
            self._perf.event(
                "00_timeline.txt",
                "replace remaining plan from validation",
                completed_prefix=self._current_step,
                new_total_steps=self._total_steps,
            )

    def _apply_result(self, result: dict):
        """Translate API-space result into logical CSS pixels for the renderer.

        The model returns coordinates in (scaled_w × scaled_h) space.
        We convert to logical CSS pixels using:
            css = model_coord * (logical_px / scaled_px)
        This works regardless of whether mss captures at physical or logical resolution.
        """
        x, y = result.get("x"), result.get("y")
        box_x, box_y = result.get("box_x"), result.get("box_y")
        w, h = result.get("w"), result.get("h")

        # Compute ratio from API coordinate space → logical CSS pixels
        ratio_x = (self._logical_w / self._scaled_w) if self._scaled_w and self._logical_w else 1.0
        ratio_y = (self._logical_h / self._scaled_h) if self._scaled_h and self._logical_h else 1.0
        logger.debug("Coord ratio x=%.3f y=%.3f  logical=%dx%d  scaled=%dx%d",
                     ratio_x, ratio_y, self._logical_w, self._logical_h,
                     self._scaled_w, self._scaled_h)

        if box_x is not None and box_y is not None:
            self._display_left = box_x * ratio_x
            self._display_top = box_y * ratio_y
            self._display_x = x * ratio_x if x is not None else None
            self._display_y = y * ratio_y if y is not None else None
            logger.debug(
                "Model box (%s, %s, %s, %s) → CSS left/top (%.1f, %.1f)",
                box_x, box_y, w, h, self._display_left, self._display_top,
            )
        elif x is not None and y is not None:
            self._display_x = x * ratio_x
            self._display_y = y * ratio_y
            self._display_left = None
            self._display_top = None
            # If mss captures only the work area (below menu bar + notch), its y=0 corresponds
            # to CSS y=workAreaY. Add the offset so the box lands on the right pixel row.
            # Log both so we can verify whether the offset is needed.
            logger.debug("Model coord (%s, %s) → CSS (%.1f, %.1f)  workAreaY=%d",
                         x, y, self._display_x, self._display_y, self._work_area_y)
        else:
            self._display_x = None
            self._display_y = None
            self._display_left = None
            self._display_top = None

        if w is not None and h is not None:
            self._display_w = w * ratio_x
            self._display_h = h * ratio_y
        else:
            # Fallback sizes in API coordinate space (scaled to CSS via ratio)
            action = result.get("action_type", "click")
            if action in ("type", "key"):
                fallback_w, fallback_h = 220, 28
            else:
                fallback_w, fallback_h = 120, 24
            self._display_w = fallback_w * ratio_x
            self._display_h = fallback_h * ratio_y

        self._display_instruction = result.get("instruction", "")
        self._display_action_type = result.get("action_type", "click")
        self._display_status = result.get("status", "confirmed")

    async def _do_displaying(self):
        has_box = self._display_left is not None and self._display_top is not None
        has_point = self._display_x is not None and self._display_y is not None
        if not has_box and not has_point:
            await self._ws_send(WS_EVENTS["error"], {"message": "No coordinates for this step"})
            self._state = IDLE
            return

        await self._ws_send(WS_EVENTS["step"], {
            "instruction": self._display_instruction,
            "x": self._display_x,
            "y": self._display_y,
            "left": self._display_left,
            "top": self._display_top,
            "w": self._display_w,
            "h": self._display_h,
            "action_type": self._display_action_type,
            "step_num": self._current_step,
            "total_steps": self._total_steps,
        })
        if self._perf:
            self._perf.event("00_timeline.txt", f"WS step event sent step={self._current_step}/{self._total_steps}")

        await self._transition(WAITING_FOR_CHANGE)

    def _do_waiting(self):
        """Start diff monitor — it will call back when screen settles."""
        if self._diff_monitor:
            self._diff_monitor.stop()
        if self._click_monitor:
            self._click_monitor.stop()

        wait_file = f"05_waiting_after_step_{self._current_step:02d}_displayed.txt"
        if self._perf:
            self._perf.event(wait_file, f"state WAITING_FOR_CHANGE: start DiffMonitor (instruction step {self._current_step})")
        focus_rect = {
            "left": self._display_left if self._display_left is not None else max(0, self._display_x - self._display_w / 2),
            "top": self._display_top if self._display_top is not None else max(0, self._display_y - self._display_h / 2),
            "width": self._display_w,
            "height": self._display_h,
        } if self._display_w is not None and self._display_h is not None and (self._display_left is not None or self._display_x is not None) and (self._display_top is not None or self._display_y is not None) else None

        if self._display_action_type in CLICK_ACTIONS and focus_rect is not None:
            if self._perf:
                self._perf.event(
                    wait_file,
                    f"state WAITING_FOR_CHANGE: start ClickMonitor (step {self._current_step})",
                    action_type=self._display_action_type,
                )
            self._click_monitor = ClickMonitor(
                focus_rect=focus_rect,
                on_click_inside=self._on_target_clicked,
                on_idle_timeout=self._on_idle_timeout,
                perf_session=self._perf,
                perf_log_file=wait_file,
            )
            self._click_monitor.start()
            return

        if self._perf:
            self._perf.event(
                wait_file,
                "state WAITING_FOR_CHANGE: start DiffMonitor fallback",
                action_type=self._display_action_type,
            )
        self._diff_monitor = DiffMonitor(
            capture_fn=capture_raw_array,
            on_settled=self._on_screen_settled,
            on_idle_timeout=self._on_idle_timeout,
            ws_send=self._ws_send,
            last_claude_call_time=self._last_claude_call_time,
            perf_session=self._perf,
            perf_log_file=wait_file,
            focus_rect=focus_rect,
            logical_size=(self._logical_w, self._logical_h),
        )
        self._diff_monitor.start()

    async def _on_target_clicked(self):
        """Called by ClickMonitor when the user clicks inside the target rect."""
        if self._cancelled:
            return
        if self._maybe_auto_advance_without_validation():
            return
        if self._perf:
            self._perf.event("00_timeline.txt", "_on_target_clicked: scheduling VALIDATING")
        asyncio.ensure_future(self._transition(VALIDATING))

    async def _on_screen_settled(self, settled_frame):
        """Called by DiffMonitor when the screen has changed and settled."""
        if self._cancelled:
            return
        if self._maybe_auto_advance_without_validation():
            return
        if self._perf:
            self._perf.event("00_timeline.txt", "_on_screen_settled: scheduling VALIDATING")
        # Schedule on a new task so the CancelledError from _diff_monitor.stop()
        # (called inside _do_validating) doesn't propagate back up through _poll_loop
        # and silently kill the validation before the HTTP request is even made.
        asyncio.ensure_future(self._transition(VALIDATING))

    def _maybe_auto_advance_without_validation(self) -> bool:
        """Advance locally when the next plan step doesn't need a new grounded target.

        The most common case is click/focus an input -> type into that same input.
        Reusing the existing box avoids an unnecessary CU validation call and keeps
        progression responsive under tight image-token rate limits.
        """
        if self._current_step <= 0 or self._current_step >= len(self._steps):
            return False

        current_idx = self._current_step - 1
        next_idx = self._current_step
        current_step = self._steps[current_idx]
        next_step = self._steps[next_idx]
        current_action = current_step.get("action")
        next_action = next_step.get("action")

        if current_action != "click" or next_action not in {"type", "key"}:
            return False

        if self._perf:
            self._perf.event("00_timeline.txt", "auto_advance_without_validation (click→type/key) skip CU")
        self._current_step += 1
        self._display_instruction = next_step.get("instruction", self._display_instruction)
        self._display_action_type = next_action
        asyncio.ensure_future(self._transition(DISPLAYING))
        return True

    async def _on_idle_timeout(self):
        """Called when no screen change detected for IDLE_TIMEOUT_MS."""
        # Re-display the current step with a nudge
        await self._ws_send(WS_EVENTS["step"], {
            "instruction": f"Still waiting... {self._display_instruction}",
            "x": self._display_x,
            "y": self._display_y,
            "left": self._display_left,
            "top": self._display_top,
            "w": self._display_w,
            "h": self._display_h,
            "action_type": self._display_action_type,
            "step_num": self._current_step,
            "total_steps": self._total_steps,
        })

    async def _do_validating(self):
        vf = f"03_validation_after_step_{self._current_step:02d}.txt"
        if self._perf:
            self._perf.event(vf, "state _do_validating: WS loading=true")
        await self._ws_send(WS_EVENTS["loading"], {"active": True})

        if self._diff_monitor:
            if self._perf:
                self._perf.event(vf, "DiffMonitor.stop()")
            self._diff_monitor.stop()
        if self._click_monitor:
            if self._perf:
                self._perf.event(vf, "ClickMonitor.stop()")
            self._click_monitor.stop()

        # Hide overlay so Claude gets a clean view of the user's screen.
        b64, sw, sh, scale = await capture_with_overlay_hidden(
            self._ws_send, self._dpr, perf=self._perf, phase_file=vf,
        )
        self._scale_factor = scale
        self._scaled_w = sw
        self._scaled_h = sh

        # Use the instruction actually shown to the user, not the pre-planned one.
        current_instruction = self._display_instruction

        # Only pass completed steps to validation — hiding future plan steps prevents the
        # model from anchoring on them and forces it to reason from actual screen state.
        completed_steps = [s for s in (self._plan or {}).get("steps", []) if s.get("n", 0) <= self._current_step]
        validation_plan = {**(self._plan or {}), "steps": completed_steps}

        if self._perf:
            self._perf.event(vf, "asyncio.to_thread(validation_call) start")
        result = await asyncio.to_thread(
            validation_call,
            b64,
            self._goal,
            validation_plan,
            self._current_step,
            current_instruction,
            [],
            sw,
            sh,
            self._dpr,
            self._perf,
        )
        self._last_claude_call_time = time.time()

        # Append to message history
        self._messages.append({
            "role": "assistant",
            "content": [{"type": "text", "text": result.get("raw_text", "")}],
        })

        status = result.get("status", "confirmed")
        self._replace_remaining_plan(result.get("remaining_plan"))
        await self._ws_send(WS_EVENTS["loading"], {"active": False})

        if status == "complete":
            await self._transition(GOAL_CHECK)
        elif status == "retry":
            self._apply_result(result)
            # Re-show same step with plan instruction
            idx = self._current_step - 1
            if 0 <= idx < len(self._steps):
                self._display_instruction = self._steps[idx]["instruction"]
            await self._transition(DISPLAYING)
        elif status == "replan":
            # CU model provides a new ad-hoc step — keep its instruction
            self._current_step += 1
            self._apply_result(result)
            await self._transition(DISPLAYING)
        else:
            # confirmed — advance
            self._current_step += 1
            self._apply_result(result)
            # Validation's instruction is grounded in the actual current screen; only fall
            # back to the pre-planned instruction when validation returned none.
            if not self._display_instruction:
                idx = self._current_step - 1
                if 0 <= idx < len(self._steps):
                    self._display_instruction = self._steps[idx]["instruction"]
            # When the pre-generated plan is exhausted, verify the goal before continuing.
            if self._current_step > self._total_steps:
                await self._transition(GOAL_CHECK)
            else:
                await self._transition(DISPLAYING)

    async def _do_goal_check(self):
        gc_file = "06_goal_check.txt"
        if self._perf:
            self._perf.event(gc_file, "state _do_goal_check: WS loading=true")
        await self._ws_send(WS_EVENTS["loading"], {"active": True})

        # Use the overlay-hidden capture so the Navi windows don't pollute the goal check.
        b64, gc_w, gc_h, gc_scale = await capture_with_overlay_hidden(
            self._ws_send, self._dpr, perf=self._perf, phase_file=gc_file,
        )
        if self._perf:
            self._perf.event(gc_file, "capture done, running goal_check_call")
        result = await asyncio.to_thread(goal_check_call, b64, self._goal, self._perf)

        await self._ws_send(WS_EVENTS["loading"], {"active": False})

        if result["achieved"]:
            await self._ws_send(WS_EVENTS["confirm_done"], {"reasoning": result["reasoning"]})
            # Wait for user_confirmed_done or user_continue event
        else:
            # Goal not yet achieved — the plan was incomplete or a step was missed.
            # Re-plan from the current screen state so the new plan reflects what is
            # actually on screen right now (e.g. an open file picker, a missing step, etc.)
            if self._replan_count >= _MAX_REPLANS:
                await self._ws_send(WS_EVENTS["error"], {
                    "message": f"Could not complete goal after {_MAX_REPLANS} re-plans. {result['reasoning']}",
                })
                self._state = IDLE
                return

            self._replan_count += 1
            if self._perf:
                self._perf.event(gc_file, f"NOT_ACHIEVED — re-plan #{self._replan_count}: {result['reasoning']}")

            await self._ws_send(WS_EVENTS["loading"], {"active": True})
            plan, grounding = await asyncio.to_thread(
                planning_and_grounding_call,
                b64,  # reuse the already-captured clean screenshot
                self._goal,
                gc_w,
                gc_h,
                self._dpr,
                self._perf,
            )
            self._last_claude_call_time = time.time()

            self._plan = plan
            self._steps = plan.get("steps", [])
            self._total_steps = len(self._steps)
            self._scaled_w = gc_w
            self._scaled_h = gc_h
            self._scale_factor = gc_scale
            self._messages = []

            if not self._steps:
                await self._ws_send(WS_EVENTS["error"], {"message": "Re-plan produced no steps"})
                self._state = IDLE
                return

            self._current_step = 1
            self._apply_result(grounding)
            self._display_instruction = self._steps[0]["instruction"]

            if self._perf:
                self._perf.event(gc_file, f"re-plan #{self._replan_count} done: {self._total_steps} steps")
            await self._ws_send(WS_EVENTS["loading"], {"active": False})
            await self._transition(DISPLAYING)
