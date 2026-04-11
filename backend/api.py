import json
import logging
import re
import time
import ast

import anthropic

from backend.perf_log import PerfSession
from shared.constants import (
    API_RETRY_MAX,
    CU_BETA_FLAG,
    GOAL_CHECK_MODEL,
    PLANNING_MODEL,
)

logger = logging.getLogger(__name__)

client = anthropic.Anthropic(max_retries=0, timeout=45.0)
CACHE_CONTROL = {"type": "ephemeral"}

PLANNING_SYSTEM = """You are Navi, a step-by-step software navigation assistant.

Given a screenshot and a goal, produce the shortest sequence of direct UI steps to accomplish it.

RULES:
- If the goal is phrased as a question ("how do I...", "where is..."), treat it as an action to perform directly using the application's native controls — do NOT suggest asking an AI assistant or opening a chat.
- Use the application's own UI: menus, toolbars, status bars, settings dialogs, keyboard shortcuts.
- Do not suggest AI chat interfaces or help/search features unless the task explicitly asks for them.

Respond with ONLY valid JSON, nothing else:
{
  "steps": [
    {
      "n": <step number>,
      "instruction": "<imperative sentence: Click..., Type..., Press...>",
      "action": "<click | type | scroll | drag | key>",
      "target": "<exact label of the UI element and its location on screen>",
      "type_text": "<text to type if action=type, else null>"
    }
  ]
}

Write every instruction in second-person imperative. Be specific about WHERE the element is."""

GOAL_CHECK_SYSTEM = """You are evaluating whether a user's task has been fully completed.

Goal: {goal}

Look at the screenshot and respond with exactly one of:
ACHIEVED
NOT_ACHIEVED: <one sentence explaining what is still missing>

Judge only by what is visible on screen right now."""


def _parse_json_response(text: str) -> dict:
    """Extract and parse the first JSON object from model output.

    Handles: preamble text before the JSON, markdown code fences, and clean JSON.
    """
    # Remove all markdown fences anywhere in the text
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```", "", text).strip()

    # Skip any preamble text before the JSON object/array
    brace = text.find('{')
    bracket = text.find('[')
    if brace == -1 and bracket == -1:
        raise ValueError("No JSON object found in response")
    start = min((i for i in (brace, bracket) if i != -1))

    return json.loads(text[start:])


def _make_screenshot_block(b64_image: str) -> dict:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": b64_image,
        },
    }


def _compact_json(data: dict) -> str:
    return json.dumps(data, separators=(",", ":"))


def _extract_labeled_object(text: str, label: str) -> dict | None:
    marker = f"{label}:"
    start = text.find(marker)
    if start == -1:
        return None

    brace_start = text.find("{", start)
    if brace_start == -1:
        return None

    depth = 0
    for idx in range(brace_start, len(text)):
        char = text[idx]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                raw_obj = text[brace_start:idx + 1]
                for parser in (json.loads, ast.literal_eval):
                    try:
                        parsed = parser(raw_obj)
                    except Exception:
                        continue
                    if isinstance(parsed, dict):
                        return parsed
                return None
    return None


def _serialize_response_content(response) -> tuple[list, str | None]:
    assistant_content = []
    tool_use_id = None
    for block in response.content:
        if block.type == "tool_use":
            tool_use_id = block.id
            assistant_content.append({
                "type": "tool_use",
                "id": block.id,
                "name": block.name,
                "input": block.input,
            })
        elif block.type == "text":
            assistant_content.append({"type": "text", "text": block.text})
    return assistant_content, tool_use_id


def _has_explicit_box(result: dict) -> bool:
    return all(
        result.get(key) is not None
        for key in ("box_x", "box_y", "w", "h")
    )


def _merge_with_last_point(result: dict, last_point: tuple[int, int] | None, last_action: str | None) -> dict:
    if last_point and result.get("x") is None and _has_explicit_box(result):
        result["x"], result["y"] = last_point
        if last_action:
            result["action_type"] = last_action
    return result


def _log_usage(perf: PerfSession | None, log_file: str, response, label: str) -> None:
    if not perf:
        return
    usage = getattr(response, "usage", None)
    inp = getattr(usage, "input_tokens", None) if usage else None
    out = getattr(usage, "output_tokens", None) if usage else None
    sr = getattr(response, "stop_reason", None)
    perf.event(log_file, label, stop_reason=str(sr) if sr is not None else "", input_tokens=inp or "", output_tokens=out or "")


def _rate_limit_sleep_seconds(error: anthropic.RateLimitError, attempt: int) -> float:
    """Use API-provided retry-after when available; otherwise use a short backoff."""
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if headers:
        retry_after = headers.get("retry-after")
        if retry_after:
            try:
                return max(0.5, float(retry_after))
            except (TypeError, ValueError):
                pass
    return float(min(8, 2 * (attempt + 1)))


def planning_call(b64_screenshot: str, goal: str, perf: PerfSession | None = None) -> dict:
    """Planning call: screenshot + goal -> JSON step array. No CU tool."""
    log_file = "01_planning.txt"
    last_err = None
    for attempt in range(API_RETRY_MAX):
        try:
            if perf:
                perf.event(log_file, f"attempt {attempt + 1}/{API_RETRY_MAX} Anthropic messages.create (no tools) start")
            t0 = time.perf_counter()
            response = client.messages.create(
                model=PLANNING_MODEL,
                max_tokens=1024,
                cache_control=CACHE_CONTROL,
                system=PLANNING_SYSTEM,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            _make_screenshot_block(b64_screenshot),
                            {"type": "text", "text": f"Goal: {goal}"},
                        ],
                    }
                ],
            )
            if perf:
                perf.event(log_file, f"attempt {attempt + 1} HTTP response received", ms=round((time.perf_counter() - t0) * 1000, 1))
            _log_usage(perf, log_file, response, "planning response metadata")
            text = ""
            for block in response.content:
                if hasattr(block, "text"):
                    text = block.text
                    break
            logger.debug("Planning raw response: %r", text[:300])
            if not text:
                raise ValueError(f"Empty planning response. stop_reason={response.stop_reason}")
            if perf:
                perf.event(log_file, "_parse_json_response (local)")
            return _parse_json_response(text)
        except anthropic.RateLimitError as e:
            wait = _rate_limit_sleep_seconds(e, attempt)
            logger.warning("Rate limited on planning attempt %d — waiting %.1fs", attempt + 1, wait)
            if perf:
                perf.event(log_file, f"RateLimitError — sleeping {wait}s before retry")
            time.sleep(wait)
            last_err = e
        except (json.JSONDecodeError, KeyError, IndexError, ValueError) as e:
            logger.warning("Planning parse failed attempt %d: %s", attempt + 1, e)
            last_err = e
            if attempt < API_RETRY_MAX - 1:
                if perf:
                    perf.event(log_file, "parse error — sleep 2s before retry")
                time.sleep(2)
    raise RuntimeError(f"Planning call failed after {API_RETRY_MAX} attempts: {last_err}")


def planning_and_grounding_call(
    b64_screenshot: str,
    goal: str,
    scaled_w: int,
    scaled_h: int,
    dpr: float = 2.0,
    perf: PerfSession | None = None,
) -> tuple[dict, dict]:
    """Plan the task AND ground step 1's coordinates in a single CU API call.

    Returns (plan_dict, grounding_result_dict).
    Saves one full API round-trip + one screen-capture cycle vs the sequential
    planning_call → grounding_call flow.
    """
    log_file = "01_planning_and_grounding.txt"

    system_prompt = f"""You are Navi. Given a screenshot and goal, do both of these in one response:
1. Plan the shortest sequence of UI steps
2. Use the computer tool's left_click action to point at step 1's element

The screenshot is already in this message — do NOT call the screenshot action.

Your text response MUST contain exactly:
PLAN:{{"steps":[{{"n":1,"instruction":"...","action":"click|type|scroll|drag|key","target":"...","type_text":null}},...]}}
USER_INSTRUCTION: <imperative sentence for step 1>
BOX:{{"x":<left_px>,"y":<top_px>,"w":<width_px>,"h":<height_px>}}

Rules:
- PLAN must be valid JSON on a single line starting with PLAN:
- Write every step instruction in second-person imperative (Click..., Type..., Press...)
- BOX must describe the exact visible element rectangle in screenshot coordinates
- Do NOT suggest AI assistants or help/search features unless the task explicitly requires them"""

    tools = [{"type": "computer_20251124", "name": "computer",
               "display_width_px": scaled_w, "display_height_px": scaled_h}]

    messages = [
        {
            "role": "user",
            "content": [
                _make_screenshot_block(b64_screenshot),
                {"type": "text", "text": f"Goal: {goal}\n\nPlan the steps and point at step 1's element using left_click."},
            ],
        }
    ]

    last_point: tuple[int, int] | None = None
    last_action: str | None = None

    def _extract_plan(text: str) -> dict | None:
        marker = "PLAN:"
        start = text.find(marker)
        if start == -1:
            return None
        brace = text.find("{", start)
        if brace == -1:
            return None
        depth = 0
        for idx in range(brace, len(text)):
            if text[idx] == "{":
                depth += 1
            elif text[idx] == "}":
                depth -= 1
                if depth == 0:
                    for parser in (json.loads, ast.literal_eval):
                        try:
                            parsed = parser(text[brace:idx + 1])
                            if isinstance(parsed, dict) and "steps" in parsed:
                                return parsed
                        except Exception:
                            pass
                    return None
        return None

    for iteration in range(5):
        try:
            if perf:
                perf.event(log_file, f"iter {iteration} beta.messages.create (plan+ground) start",
                           display_px=f"{scaled_w}x{scaled_h}")
            t0 = time.perf_counter()
            response = client.beta.messages.create(
                model=PLANNING_MODEL,
                max_tokens=1024,
                cache_control=CACHE_CONTROL,
                betas=[CU_BETA_FLAG],
                system=system_prompt,
                tools=tools,
                messages=messages,
            )
            if perf:
                perf.event(log_file, f"iter {iteration} HTTP response",
                           ms=round((time.perf_counter() - t0) * 1000, 1))
            _log_usage(perf, log_file, response, f"plan+ground iter {iteration} metadata")
        except anthropic.RateLimitError as e:
            wait = _rate_limit_sleep_seconds(e, iteration)
            logger.warning("Planning+grounding rate limited — waiting %.1fs", wait)
            if perf:
                perf.event(log_file, f"RateLimitError — sleep {wait}s")
            time.sleep(wait)
            continue

        result = _parse_cu_response(response)
        if result["x"] is not None and result["y"] is not None:
            last_point = (result["x"], result["y"])
            last_action = result.get("action_type")
        result = _merge_with_last_point(result, last_point, last_action)

        raw_text = result.get("raw_text", "")
        plan = _extract_plan(raw_text)

        if plan and result["x"] is not None and _has_explicit_box(result):
            if perf:
                perf.event(log_file, f"iter {iteration} done (plan + coords + BOX)",
                           steps=len(plan.get("steps", [])))
            return plan, result

        # Plan + coords but no BOX — ask for the rectangle without a new screenshot.
        if plan and result["x"] is not None:
            if perf:
                perf.event(log_file, f"iter {iteration} branch: coords without BOX — requesting rectangle")
            assistant_content, tool_use_id = _serialize_response_content(response)
            messages.append({"role": "assistant", "content": assistant_content})
            if tool_use_id:
                messages.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": [{
                            "type": "text",
                            "text": (
                                "Navi intercepted that click and did not execute it. "
                                "For the SAME target, reply now with only:\n"
                                "USER_INSTRUCTION: ...\n"
                                "BOX:{\"x\": ..., \"y\": ..., \"w\": ..., \"h\": ...}\n"
                                "Do not call any additional tool."
                            ),
                        }],
                    }],
                })
            continue

        # Have plan but model hasn't clicked yet
        if plan and result["x"] is None:
            step_1_instr = plan.get("steps", [{}])[0].get("instruction", "the first element")
            if result["action_type"] == "screenshot":
                if perf:
                    perf.event(log_file, f"iter {iteration}: plan ok, screenshot action — feed image + ask for click")
                assistant_content, tool_use_id = _serialize_response_content(response)
                messages.append({"role": "assistant", "content": assistant_content})
                messages.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": tool_use_id,
                     "content": [_make_screenshot_block(b64_screenshot)]},
                    {"type": "text", "text": f"Now use left_click to point at: {step_1_instr}"},
                ]})
            else:
                if perf:
                    perf.event(log_file, f"iter {iteration}: plan ok, no click — nudge")
                assistant_content = [{"type": "text", "text": raw_text}] if raw_text else []
                messages.append({"role": "assistant", "content": assistant_content})
                messages.append({"role": "user", "content": [
                    {"type": "text", "text": f"Good plan. Now use left_click to point at: {step_1_instr}"},
                ]})
            continue

        # No plan yet — handle screenshot request or nudge
        if result["action_type"] == "screenshot":
            if perf:
                perf.event(log_file, f"iter {iteration}: screenshot before plan — feed image")
            assistant_content, tool_use_id = _serialize_response_content(response)
            messages.append({"role": "assistant", "content": assistant_content})
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tool_use_id,
                 "content": [_make_screenshot_block(b64_screenshot)]},
            ]})
            continue

        if perf:
            perf.event(log_file, f"iter {iteration}: no plan, no coords — nudge")
        assistant_content = [{"type": "text", "text": raw_text}] if raw_text else []
        messages.append({"role": "assistant", "content": assistant_content})
        messages.append({"role": "user", "content": [
            {"type": "text", "text": "Respond with PLAN:{...} JSON and use left_click to point at step 1's element."},
        ]})

    if perf:
        perf.event(log_file, "exhausted iterations — returning best effort")
    return plan or {}, result


def grounding_call(
    b64_screenshot: str,
    goal: str,
    plan: dict,
    step_1_instruction: str,
    scaled_w: int,
    scaled_h: int,
    dpr: float = 2.0,
    perf: PerfSession | None = None,
) -> dict:
    """Grounding call: CU tool call to get coordinates for step 1.

    Handles the CU screenshot loop: if the model requests a screenshot first,
    capture one and continue the conversation until we get coordinates.
    """
    system_prompt = f"""You are Navi, a navigation guide assistant. You help users by identifying exactly where on their screen they need to click.

Your role: use the computer tool to point at the exact UI element the user needs. You are NOT clicking — you are identifying the location so a box can be drawn for the user.

Goal: {goal}
Plan:
{_compact_json(plan)}

Step 1 to locate: {step_1_instruction}

The screenshot is already in this message — do NOT use the screenshot action. Your first tool call must be left_click.
Use the computer tool's click action to point at the element.
You MUST also include a text response (not just the tool call) containing exactly:
USER_INSTRUCTION: <imperative sentence for the user>
BOX:{{"x": <left_px>, "y": <top_px>, "w": <width_px>, "h": <height_px>}}

BOX must describe the exact visible element rectangle in screenshot coordinates.
The computer tool click coordinate may be anywhere inside that rectangle, so do not assume it is centered."""

    tools = [
        {
            "type": "computer_20251124",
            "name": "computer",
            "display_width_px": scaled_w,
            "display_height_px": scaled_h,
        }
    ]

    messages = [
        {
            "role": "user",
            "content": [
                _make_screenshot_block(b64_screenshot),
                {"type": "text", "text": f"Perform the action for Step 1: {step_1_instruction}"},
            ],
        }
    ]
    last_point: tuple[int, int] | None = None
    last_action: str | None = None
    log_file = "02_grounding.txt"

    for iteration in range(5):
        try:
            if perf:
                perf.event(log_file, f"iter {iteration} beta.messages.create (CU tool) start", display_px=f"{scaled_w}x{scaled_h}")
            t0 = time.perf_counter()
            response = client.beta.messages.create(
                model=PLANNING_MODEL,
                max_tokens=512,  # was 1024 — response is 3 structured lines; increase if truncated
                cache_control=CACHE_CONTROL,
                betas=[CU_BETA_FLAG],
                system=system_prompt,
                tools=tools,
                messages=messages,
            )
            if perf:
                perf.event(log_file, f"iter {iteration} HTTP response", ms=round((time.perf_counter() - t0) * 1000, 1))
            _log_usage(perf, log_file, response, f"grounding iter {iteration} metadata")
        except anthropic.RateLimitError as e:
            wait = _rate_limit_sleep_seconds(e, iteration)
            logger.warning("Grounding rate limited — waiting %.1fs", wait)
            if perf:
                perf.event(log_file, f"RateLimitError — sleep {wait}s")
            time.sleep(wait)
            continue

        result = _parse_cu_response(response)
        if result["x"] is not None and result["y"] is not None:
            last_point = (result["x"], result["y"])
            last_action = result.get("action_type")
        result = _merge_with_last_point(result, last_point, last_action)
        logger.debug("Grounding iter %d: action=%s x=%s y=%s text=%r",
                     iteration, result["action_type"], result["x"], result["y"],
                     result["raw_text"][:120] if result["raw_text"] else "")

        # Coordinates + explicit BOX — done in one call.
        if result["x"] is not None and _has_explicit_box(result):
            if perf:
                perf.event(log_file, f"iter {iteration} done (coords + BOX)", action=result.get("action_type"))
            return result

        # Coordinates but no BOX — the fallback dims are too imprecise; ask for BOX only.
        # The model sees its own prior tool_use as a tool_result, no new screenshot needed.
        if result["x"] is not None:
            if perf:
                perf.event(log_file, f"iter {iteration} branch: coords without BOX — requesting rectangle")
            assistant_content, tool_use_id = _serialize_response_content(response)
            messages.append({"role": "assistant", "content": assistant_content})
            if tool_use_id:
                messages.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": [{
                            "type": "text",
                            "text": (
                                "Navi intercepted that click and did not execute it. "
                                "For the SAME target, reply now with only:\n"
                                "USER_INSTRUCTION: ...\n"
                                "BOX:{\"x\": ..., \"y\": ..., \"w\": ..., \"h\": ...}\n"
                                "Do not call any additional tool."
                            ),
                        }],
                    }],
                })
            continue

        # Model took a screenshot action — provide the clean screenshot and continue
        if result["action_type"] == "screenshot":
            logger.debug("Grounding iter %d: model requested screenshot", iteration + 1)
            if perf:
                perf.event(log_file, f"iter {iteration} branch: model used screenshot tool (extra round-trip)")
            assistant_content, tool_use_id = _serialize_response_content(response)
            messages.append({"role": "assistant", "content": assistant_content})
            # Always reuse the clean pre-captured screenshot — never capture live here
            # because the Navi overlay would be visible and confuse the model.
            messages.append({
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": tool_use_id,
                              "content": [_make_screenshot_block(b64_screenshot)]}],
            })
            continue

        # No coordinates and no screenshot action — nudge the model to use the tool
        logger.warning("Grounding iter %d: no coordinates in response, nudging model", iteration + 1)
        if perf:
            perf.event(log_file, f"iter {iteration} branch: no coords — nudge model")
        assistant_content = [{"type": "text", "text": result["raw_text"]}] if result["raw_text"] else []
        messages.append({"role": "assistant", "content": assistant_content})
        messages.append({
            "role": "user",
            "content": [{"type": "text", "text": "You must use the computer tool with a click action to identify the element location. Please point at the element now."}],
        })

    if perf:
        perf.event(log_file, "exhausted iterations (5) — returning last parse")
    return result


def validation_call(
    b64_screenshot: str,
    goal: str,
    plan: dict,
    step_n: int,
    step_instruction: str,
    messages: list,
    scaled_w: int,
    scaled_h: int,
    dpr: float = 2.0,
    perf: PerfSession | None = None,
) -> dict:
    """Validation call: check if step N completed and get next action."""
    system_prompt = f"""You are Navi, a navigation guide assistant. You identify WHERE the user needs to interact next.

Goal: {goal}
Plan: {_compact_json(plan)}
Step just completed by the user: Step {step_n} — {step_instruction}

The screenshot is already in this message — do NOT use the screenshot action. Your first tool call must be left_click.
Look at the screenshot and:
1. Determine if Step {step_n} completed successfully (did the screen change as expected?)
2. Identify where the user needs to interact NEXT by using the computer tool's click action to point at the next element.

You MUST include a text response (not just the tool call) containing exactly:
USER_INSTRUCTION: <imperative sentence for the user>
BOX:{{"x": <left_px>, "y": <top_px>, "w": <width_px>, "h": <height_px>}}
STATUS:<confirmed|retry|replan|complete>

Status meanings:
  confirmed — step completed, pointing at next planned step
  retry     — step did not complete, pointing at same element again
  replan    — unexpected screen state, your tool_use replaces the remaining plan
  complete  — goal fully achieved, omit tool_use for this status

If the next element is not visible, set STATUS:replan and describe what to do."""

    tools = [
        {
            "type": "computer_20251124",
            "name": "computer",
            "display_width_px": scaled_w,
            "display_height_px": scaled_h,
        }
    ]

    # Build messages: include prior history + new screenshot
    all_messages = list(messages)
    all_messages.append(
        {
            "role": "user",
            "content": [
                _make_screenshot_block(b64_screenshot),
                {
                    "type": "text",
                    "text": f"The user has performed Step {step_n}. Here is the current screen. What should they do next?",
                },
            ],
        }
    )
    last_point: tuple[int, int] | None = None
    last_action: str | None = None
    log_file = f"03_validation_after_step_{step_n:02d}.txt"
    if perf:
        perf.event(log_file, "validation_call entry", prior_user_messages=len(messages))

    for iteration in range(5):
        try:
            if perf:
                perf.event(log_file, f"iter {iteration} beta.messages.create (CU validation) start", step=step_n)
            t0 = time.perf_counter()
            response = client.beta.messages.create(
                model=PLANNING_MODEL,
                max_tokens=512,  # was 1024 — response is 3 structured lines; increase if truncated
                cache_control=CACHE_CONTROL,
                betas=[CU_BETA_FLAG],
                system=system_prompt,
                tools=tools,
                messages=all_messages,
            )
            if perf:
                perf.event(log_file, f"iter {iteration} HTTP response", ms=round((time.perf_counter() - t0) * 1000, 1))
            _log_usage(perf, log_file, response, f"validation iter {iteration} metadata")
        except anthropic.RateLimitError as e:
            wait = _rate_limit_sleep_seconds(e, iteration)
            logger.warning("Validation rate limited — waiting %.1fs", wait)
            if perf:
                perf.event(log_file, f"RateLimitError — sleep {wait}s")
            time.sleep(wait)
            continue

        result = _parse_cu_response(response)
        if result["x"] is not None and result["y"] is not None:
            last_point = (result["x"], result["y"])
            last_action = result.get("action_type")
        result = _merge_with_last_point(result, last_point, last_action)
        logger.debug("Validation iter %d: status=%s action=%s x=%s y=%s text=%r",
                     iteration, result["status"], result["action_type"], result["x"], result["y"],
                     result["raw_text"][:120] if result["raw_text"] else "")

        if result["status"] == "complete":
            if perf:
                perf.event(log_file, f"iter {iteration} done STATUS=complete")
            return result

        # Coordinates + explicit BOX — done.
        if result["x"] is not None and _has_explicit_box(result):
            if perf:
                perf.event(log_file, f"iter {iteration} done (coords + BOX)", status=result.get("status"))
            return result

        # Coordinates but no BOX — request the rectangle without a new screenshot.
        if result["x"] is not None:
            if perf:
                perf.event(log_file, f"iter {iteration} branch: coords without BOX — requesting rectangle")
            assistant_content, tool_use_id = _serialize_response_content(response)
            all_messages.append({"role": "assistant", "content": assistant_content})
            if tool_use_id:
                all_messages.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": [{
                            "type": "text",
                            "text": (
                                "Navi intercepted that click and did not execute it. "
                                "For the SAME next target, reply now with only:\n"
                                "USER_INSTRUCTION: ...\n"
                                "BOX:{\"x\": ..., \"y\": ..., \"w\": ..., \"h\": ...}\n"
                                "STATUS:<confirmed|retry|replan|complete>\n"
                                "Do not call any additional tool."
                            ),
                        }],
                    }],
                })
            continue

        if result["action_type"] == "screenshot":
            logger.debug("Validation iter %d: model requested screenshot", iteration + 1)
            if perf:
                perf.event(log_file, f"iter {iteration} branch: model used screenshot tool (extra round-trip)")
            assistant_content, tool_use_id = _serialize_response_content(response)
            all_messages.append({"role": "assistant", "content": assistant_content})
            all_messages.append({
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": tool_use_id,
                              "content": [_make_screenshot_block(b64_screenshot)]}],
            })
            continue

        logger.warning("Validation iter %d: no coordinates, nudging model", iteration + 1)
        if perf:
            perf.event(log_file, f"iter {iteration} branch: no coords — nudge model")
        assistant_content = [{"type": "text", "text": result["raw_text"]}] if result["raw_text"] else []
        all_messages.append({"role": "assistant", "content": assistant_content})
        all_messages.append({
            "role": "user",
            "content": [{"type": "text", "text": "You must use the computer tool with a click action to identify the next element. Please point at it now."}],
        })

    if perf:
        perf.event(log_file, "exhausted iterations (5) — returning last parse")
    return result


def goal_check_call(b64_screenshot: str, goal: str, perf: PerfSession | None = None) -> dict:
    """Isolated goal check with Haiku. No tools, no history."""
    log_file = "06_goal_check.txt"
    if perf:
        perf.event(log_file, "goal_check_call Haiku messages.create start")
    t0 = time.perf_counter()
    response = client.messages.create(
        model=GOAL_CHECK_MODEL,
        max_tokens=256,
        cache_control=CACHE_CONTROL,
        system=GOAL_CHECK_SYSTEM.format(goal=goal),
        messages=[
            {
                "role": "user",
                "content": [
                    _make_screenshot_block(b64_screenshot),
                    {"type": "text", "text": "Is the goal achieved? Look at the screenshot."},
                ],
            }
        ],
    )
    if perf:
        perf.event(log_file, "HTTP response", ms=round((time.perf_counter() - t0) * 1000, 1))
    _log_usage(perf, log_file, response, "goal_check metadata")

    text = response.content[0].text.strip()
    achieved = text.startswith("ACHIEVED")
    if perf:
        perf.event(log_file, "parsed result", achieved=achieved)
    return {"achieved": achieved, "reasoning": text}


def _parse_cu_response(response) -> dict:
    """Extract coordinates, bounds, instruction, and status from a CU response."""
    result = {
        "x": None,
        "y": None,
        "box_x": None,
        "box_y": None,
        "w": None,
        "h": None,
        "instruction": "",
        "status": "confirmed",
        "action_type": "click",
        "raw_text": "",
    }

    for block in response.content:
        logger.debug("CU response block: type=%s input=%r", block.type, getattr(block, 'input', None))
        if block.type == "tool_use":
            inp = block.input
            logger.debug("tool_use input keys: %s", list(inp.keys()) if inp else None)
            coord = inp.get("coordinate")
            logger.debug("coordinate value: %r", coord)
            if coord:
                result["x"] = coord[0]
                result["y"] = coord[1]
            result["action_type"] = inp.get("action", "click")

        elif block.type == "text":
            text = block.text
            result["raw_text"] = text

            # Parse BOX (preferred)
            box_obj = _extract_labeled_object(text, "BOX")
            if box_obj:
                try:
                    result["box_x"] = int(round(float(box_obj["x"])))
                    result["box_y"] = int(round(float(box_obj["y"])))
                    result["w"] = int(round(float(box_obj["w"])))
                    result["h"] = int(round(float(box_obj["h"])))
                except (KeyError, TypeError, ValueError):
                    pass

            # Parse BOUNDS
            bounds_obj = _extract_labeled_object(text, "BOUNDS")
            if bounds_obj and result["w"] is None and result["h"] is None:
                try:
                    result["w"] = int(round(float(bounds_obj["w"])))
                    result["h"] = int(round(float(bounds_obj["h"])))
                except (KeyError, TypeError, ValueError):
                    pass

            # Parse STATUS
            status_match = re.search(r"STATUS:\s*(confirmed|retry|replan|complete)", text)
            if status_match:
                result["status"] = status_match.group(1)

            # Parse USER_INSTRUCTION (preferred) or fall back to first plain line
            instr_match = re.search(r"USER_INSTRUCTION:\s*(.+)", text)
            if instr_match:
                result["instruction"] = instr_match.group(1).strip()
            else:
                lines = text.strip().split("\n")
                for line in lines:
                    line = line.strip()
                    if (
                        line
                        and not line.startswith("BOX:")
                        and not line.startswith("BOUNDS:")
                        and not line.startswith("STATUS:")
                        and not line.startswith("USER_INSTRUCTION:")
                    ):
                        result["instruction"] = line
                        break

    return result
