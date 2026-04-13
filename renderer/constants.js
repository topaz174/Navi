export const COLORS = {
  accent:       '#3B82F6',
  accentDim:    '#3B82F640',
  surface:      '#0A0A0ABA',
  surfaceHover: '#1A1A1AB0',
  text:         '#F0F0F0',
  textMuted:    '#A0A0A0',
  border:       '#3B82F620',
};

export const RADIUS = {
  box:   8,
  panel: 12,
};

export const BLUR = {
  panel: 'blur(14px)',
};

export const ANIMATION = {
  glowPulseDuration:    '2s',
  stepInDuration:       '180ms',  // must stay in sync with step-in in styles.css
  loadingCycleDuration: '1.4s',
  scannerDurationMs:     280,
};

// ui timing (ms) - durations that don't map to css animations
export const UI = {
  stepTransitionDelayMs: 170,  // slightly less than stepinduration so old box fades first
  boxHideDelayMs:        130,  // slightly less than 150ms css opacity transition
  loadingHideDelayMs:    420,
  completionDisplayMs:  3000,
};

export const WS_PORT = 7373;

export const WS_EVENTS = {
  dpr:                 'dpr',
  step:                'step',
  loading:             'loading',
  confirm_done:        'confirm_done',
  done:                'done',
  error:               'error',
  goal:                'goal',
  user_confirmed_done: 'user_confirmed_done',
  user_continue:       'user_continue',
  cancel:              'cancel',
  hide:                'hide',
  show:                'show',
  voice_start:         'voice_start',
  voice_stop:          'voice_stop',
  voice_transcript:    'voice_transcript',
  voice_error:         'voice_error',
};
