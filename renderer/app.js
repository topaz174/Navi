import { WS_EVENTS } from './constants.js';

const role = document.body.dataset.role || 'control';
const isControlWindow = role === 'control';
const isOverlayWindow = role === 'overlay';

const boundingBox = document.getElementById('bounding-box');
const tooltip = document.getElementById('tooltip');
const tooltipText = document.getElementById('tooltip-text');
const stepHud = document.getElementById('step-hud');
const stepCounter = document.getElementById('step-counter');
const stepHistory = document.getElementById('step-history');
const stepAdvance = document.getElementById('step-advance');
const stepAdvanceText = document.getElementById('step-advance-text');
const loading = document.getElementById('loading');
const loadingScanner = document.getElementById('loading-scanner');
const completion = document.getElementById('completion');
const confirmDone = document.getElementById('confirm-done');
const btnYes = document.getElementById('btn-yes');
const btnContinue = document.getElementById('btn-continue');
const errorDisplay = document.getElementById('error-display');
const errorText = document.getElementById('error-text');
const errorDismiss = document.getElementById('error-dismiss');
const inputBar = document.getElementById('input-bar');
const chatLog = document.getElementById('chat-log');
const chatToggle = document.getElementById('chat-toggle');
const goalInput = document.getElementById('goal-input');
const activeGoal = document.getElementById('active-goal');
const activeGoalText = document.getElementById('active-goal-text');
const submitBtn = document.getElementById('submit-btn');
const cancelBtn = document.getElementById('cancel-btn');
const micToggle = document.getElementById('mic-toggle');

let completedSteps = [];
let isListening = false;
let overlayBounds = { x: 0, y: 0 };
let thinkingEntry = null;
let chatExpanded = false;
let lastInstruction = '';
let pendingLoadingHide = null;
let pendingStepRender = null;
let currentStepNum = 0;

function wsSend(event, payload = {}) {
  const msg = JSON.stringify({ event, payload });
  window.navi.send('ws-send', msg);
}

function setHidden(el, hidden) {
  if (!el) return;
  el.classList.toggle('hidden', hidden);
}

function appendChatEntry(roleName, text, options = {}) {
  if (!chatLog) return null;
  const entry = document.createElement('div');
  entry.className = `chat-entry ${roleName}`;

  const bubble = document.createElement('div');
  bubble.className = `chat-bubble${options.thinking ? ' thinking' : ''}`;
  bubble.textContent = text;
  entry.appendChild(bubble);
  chatLog.appendChild(entry);
  chatLog.scrollTop = chatLog.scrollHeight;

  if (options.expand) {
    setChatExpanded(true);
  }
  return entry;
}

function removeThinkingEntry() {
  if (thinkingEntry?.parentNode) {
    thinkingEntry.parentNode.removeChild(thinkingEntry);
  }
  thinkingEntry = null;
}

function clearPendingLoadingHide() {
  if (pendingLoadingHide) {
    clearTimeout(pendingLoadingHide);
    pendingLoadingHide = null;
  }
}

function resetScannerRoam() {
  if (!loadingScanner) return;
  loadingScanner.getAnimations().forEach((animation) => animation.cancel());
  loadingScanner.style.animation = '';
  loadingScanner.style.transform = '';
}

function animateScannerToTarget(targetX, targetY) {
  if (!loadingScanner) return Promise.resolve();

  const rect = loadingScanner.getBoundingClientRect();
  const startX = rect.left;
  const startY = rect.top;
  const endX = targetX - rect.width / 2;
  const endY = targetY - rect.height / 2;

  loadingScanner.getAnimations().forEach((animation) => animation.cancel());
  loadingScanner.style.animation = 'none';
  loadingScanner.style.transform = `translate(${startX}px, ${startY}px) rotate(8deg) scale(1)`;

  const animation = loadingScanner.animate(
    [
      { transform: `translate(${startX}px, ${startY}px) rotate(8deg) scale(1)` },
      { transform: `translate(${endX}px, ${endY}px) rotate(-10deg) scale(1.04)`, offset: 0.78 },
      { transform: `translate(${endX}px, ${endY}px) rotate(-5deg) scale(1)` },
    ],
    {
      duration: 280,
      easing: 'cubic-bezier(0.18, 0.88, 0.16, 1)',
      fill: 'forwards',
    }
  );

  return animation.finished.catch(() => {}).then(() => {
    loadingScanner.style.transform = `translate(${endX}px, ${endY}px) rotate(-5deg) scale(1)`;
  });
}

function setChatExpanded(expanded) {
  if (!inputBar || !chatToggle) return;
  chatExpanded = expanded;
  inputBar.classList.toggle('expanded', expanded);
  inputBar.classList.toggle('collapsed', !expanded);
  chatToggle.title = expanded ? 'Collapse chat history' : 'Expand chat history';
  chatToggle.setAttribute('aria-label', chatToggle.title);
  if (expanded && chatLog) {
    chatLog.scrollTop = chatLog.scrollHeight;
  }
}

function renderStepHistory() {
  if (!stepHistory) return;
  stepHistory.innerHTML = completedSteps.map((s) =>
    `<div class="history-item"><span class="check">&#10003;</span> Step ${s.num}: ${s.instruction}</div>`
  ).join('');
}

function clearPendingStepRender() {
  if (pendingStepRender) {
    clearTimeout(pendingStepRender);
    pendingStepRender = null;
  }
}

function flashStepAdvance(text, duration = 820) {
  if (!stepAdvance || !stepAdvanceText || !text) return;
  stepAdvanceText.textContent = text;
  stepAdvance.classList.remove('hidden', 'animate');
  stepAdvance.offsetHeight;
  stepAdvance.classList.add('animate');
  window.setTimeout(() => {
    stepAdvance.classList.add('hidden');
    stepAdvance.classList.remove('animate');
  }, duration);
}

function renderOverlayStep(screenBx, screenBy, x, y, w, h, instruction) {
  if (!(isOverlayWindow && boundingBox && tooltip && tooltipText)) return;

  const bx = screenBx - (overlayBounds.x || 0);
  const by = screenBy - (overlayBounds.y || 0);
  const anchorX = (Number.isFinite(x) ? x : screenBx + w / 2) - (overlayBounds.x || 0);

  boundingBox.style.left = `${bx}px`;
  boundingBox.style.top = `${by}px`;
  boundingBox.style.width = `${w}px`;
  boundingBox.style.height = `${h}px`;
  boundingBox.classList.remove('transitioning');
  setHidden(boundingBox, false);

  boundingBox.style.animation = 'none';
  boundingBox.offsetHeight;
  boundingBox.style.animation = '';

  tooltipText.textContent = instruction;
  setHidden(tooltip, false);

  const tooltipHeight = 50;
  const gap = 10;
  tooltip.style.top = by > tooltipHeight + gap + 20
    ? `${by - tooltipHeight - gap}px`
    : `${by + h + gap}px`;
  tooltip.style.left = `${Math.max(8, anchorX - 200)}px`;
}

function showStep(payload) {
  const { instruction, x, y, left, top, w, h, step_num, total_steps } = payload;

  if (step_num > 1 && completedSteps.length < step_num - 1) {
    const prevNum = step_num - 1;
    if (!completedSteps.find((s) => s.num === prevNum)) {
      completedSteps.push({ num: prevNum, instruction: lastInstruction });
    }
  }

  lastInstruction = instruction;

  const screenBx = Number.isFinite(left) ? left : x - w / 2;
  const screenBy = Number.isFinite(top) ? top : y - h / 2;
  const targetCenterX = screenBx - (overlayBounds.x || 0) + w / 2;
  const targetCenterY = screenBy - (overlayBounds.y || 0) + h / 2;

  clearPendingLoadingHide();
  if (isOverlayWindow && loading && !loading.classList.contains('hidden') && loadingScanner) {
    animateScannerToTarget(targetCenterX, targetCenterY).finally(() => {
      setHidden(loading, true);
      resetScannerRoam();
    });
  }

  clearPendingStepRender();
  if (isOverlayWindow && step_num > 1) {
    flashStepAdvance(`Step ${step_num} of ${total_steps}`);
    if (boundingBox) boundingBox.classList.add('transitioning');
    if (tooltip) tooltip.classList.add('hidden');
    pendingStepRender = setTimeout(() => {
      renderOverlayStep(screenBx, screenBy, x, y, w, h, instruction);
      pendingStepRender = null;
    }, 170);
  } else {
    renderOverlayStep(screenBx, screenBy, x, y, w, h, instruction);
  }

  if (stepCounter) {
    stepCounter.textContent = `Step ${step_num} of ${total_steps} — ${instruction}`;
    stepCounter.dataset.instruction = instruction;
  }
  setHidden(stepHud, false);
  renderStepHistory();
  removeThinkingEntry();
  appendChatEntry('assistant', `Step ${step_num} of ${total_steps}: ${instruction}`);
  currentStepNum = step_num;

  setHidden(confirmDone, true);
  setHidden(completion, true);
  setHidden(errorDisplay, true);
}

function toggleLoading(active) {
  clearPendingLoadingHide();

  if (loading) {
    if (active) {
      resetScannerRoam();
      setHidden(loading, false);
      if (isOverlayWindow && currentStepNum > 0) {
        if (boundingBox && !boundingBox.classList.contains('hidden')) {
          boundingBox.classList.add('transitioning');
          window.setTimeout(() => {
            setHidden(boundingBox, true);
          }, 130);
        }
        if (tooltip) {
          setHidden(tooltip, true);
        }
        flashStepAdvance('Step complete', 900);
      }
    } else if (isOverlayWindow && loadingScanner) {
      pendingLoadingHide = setTimeout(() => {
        setHidden(loading, true);
        resetScannerRoam();
        pendingLoadingHide = null;
      }, 420);
    } else {
      setHidden(loading, true);
    }
  }

  if (!chatLog) return;

  if (active) {
    if (!thinkingEntry) {
      thinkingEntry = appendChatEntry('system', 'Navi is thinking...', { thinking: true });
    }
  } else {
    removeThinkingEntry();
  }
}

function showConfirmDone() {
  setHidden(boundingBox, true);
  setHidden(tooltip, true);
  setHidden(confirmDone, false);
  appendChatEntry('assistant', 'Navi thinks the task is done. Does it look right?');
}

function showCompletion() {
  removeThinkingEntry();
  setHidden(boundingBox, true);
  setHidden(tooltip, true);
  setHidden(stepHud, true);
  setHidden(confirmDone, true);
  setHidden(completion, false);
  appendChatEntry('assistant', 'Done.');

  setTimeout(() => {
    setHidden(completion, true);
    resetToIdle();
  }, 3000);
}

function showError(message) {
  removeThinkingEntry();
  appendChatEntry('system', message);
  if (errorText) errorText.textContent = message;
  setHidden(errorDisplay, false);
}

function hideOverlay() {
  if (isOverlayWindow) {
    document.body.style.opacity = '0';
  }
}

function showOverlay() {
  if (isOverlayWindow) {
    document.body.style.opacity = '1';
  }
}

function submitGoal() {
  if (!goalInput) return;
  const text = goalInput.value.trim();
  if (!text) return;

  wsSend(WS_EVENTS.goal, { text });
  appendChatEntry('user', text);

  if (activeGoalText) activeGoalText.textContent = text;
  setHidden(activeGoal, false);
  completedSteps = [];
  lastInstruction = '';
  goalInput.value = '';
}

function resetToIdle() {
  clearPendingStepRender();
  setHidden(activeGoal, true);
  setHidden(boundingBox, true);
  setHidden(tooltip, true);
  setHidden(stepAdvance, true);
  setHidden(stepHud, true);
  setHidden(loading, true);
  setHidden(confirmDone, true);
  setHidden(errorDisplay, true);
  completedSteps = [];
  lastInstruction = '';
  currentStepNum = 0;
  if (stepHistory) stepHistory.innerHTML = '';
  removeThinkingEntry();
}

// ── Voice input (via Python backend) ──────────────
// webkitSpeechRecognition in Electron requires an authenticated Google session
// which is unavailable. Instead we send voice_start/voice_stop to the Python
// backend, which records via sounddevice and transcribes via Google Speech API,
// then sends back a voice_transcript or voice_error event.
function setupDictation() {
  if (!micToggle || !goalInput) return;

  micToggle.addEventListener('click', () => {
    if (isListening) {
      wsSend(WS_EVENTS.voice_stop);
      isListening = false;
      micToggle.classList.remove('active');
      goalInput.placeholder = 'Processing...';
    } else {
      isListening = true;
      micToggle.classList.add('active');
      goalInput.value = '';
      goalInput.placeholder = 'Listening… tap mic to stop';
      wsSend(WS_EVENTS.voice_start);
    }
  });
}

function handleWsMessage(data) {
  let msg;
  try {
    msg = JSON.parse(data);
  } catch {
    return;
  }

  const { event, payload } = msg;
  switch (event) {
    case WS_EVENTS.step:
      showStep(payload);
      break;
    case WS_EVENTS.loading:
      toggleLoading(payload.active);
      break;
    case WS_EVENTS.confirm_done:
      showConfirmDone(payload.reasoning);
      break;
    case WS_EVENTS.done:
      showCompletion();
      break;
    case WS_EVENTS.error:
      showError(payload.message);
      break;
    case WS_EVENTS.hide:
      hideOverlay();
      break;
    case WS_EVENTS.show:
      showOverlay();
      break;
    case WS_EVENTS.voice_transcript:
      isListening = false;
      if (micToggle) micToggle.classList.remove('active');
      if (goalInput) {
        goalInput.placeholder = 'Ask Navi what you want to do...';
        goalInput.value = (payload.text || '').trim();
        if (goalInput.value) {
          submitGoal();
        } else {
          goalInput.focus();
        }
      }
      break;
    case WS_EVENTS.voice_error:
      isListening = false;
      if (micToggle) micToggle.classList.remove('active');
      if (goalInput) goalInput.placeholder = 'Ask Navi what you want to do...';
      showError(payload.message);
      break;
    default:
      break;
  }
}

window.navi.on('ws-message', (data) => handleWsMessage(data));
window.navi.on('ws-connected', () => {
  console.log('[Navi] Connected to backend');
});
window.navi.on('overlay-bounds', (bounds) => {
  overlayBounds = bounds || { x: 0, y: 0 };
});
window.navi.on('ws-disconnected', () => {
  console.log('[Navi] Disconnected from backend');
  resetToIdle();
});

if (chatLog) {
  appendChatEntry('system', 'Tell Navi what you want to do, and it will guide you step by step.');
}

if (inputBar) {
  inputBar.classList.add('collapsed');
}

if (chatToggle) {
  chatToggle.addEventListener('click', () => {
    setChatExpanded(!chatExpanded);
  });
}

if (goalInput) {
  goalInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      submitGoal();
    }
  });
}

if (submitBtn) {
  submitBtn.addEventListener('click', submitGoal);
}

if (cancelBtn) {
  cancelBtn.addEventListener('click', () => {
    wsSend(WS_EVENTS.cancel);
    resetToIdle();
  });
}

if (btnYes) {
  btnYes.addEventListener('click', () => {
    setHidden(confirmDone, true);
    appendChatEntry('user', 'Yes, that looks right.');
    wsSend(WS_EVENTS.user_confirmed_done);
  });
}

if (btnContinue) {
  btnContinue.addEventListener('click', () => {
    setHidden(confirmDone, true);
    appendChatEntry('user', 'Keep going.');
    wsSend(WS_EVENTS.user_continue);
  });
}

if (errorDismiss) {
  errorDismiss.addEventListener('click', () => {
    setHidden(errorDisplay, true);
  });
}

if (isControlWindow) {
  setupDictation();
}
