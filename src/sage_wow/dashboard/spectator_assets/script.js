'use strict';
// Native shell supplies a read-only projection. No fetch, input bridge, or demo loop.
(() => {
  const $ = id => document.getElementById(id);
  const hud = $('hud');
  const card = $('decision-card');
  const FRESH_MS = 5000;
  const REACTION_MS = 2500;
  const validStates = new Set(['PLAYING', 'PAUSED', 'STOPPED', 'BLOCKED', 'STALE', 'UNAVAILABLE']);
  const activePhases = new Set(['thinking', 'decision', 'executing', 'verifying', 'observing']);
  let snapshot = null;
  let receivedAt = 0;
  let sourceKey = null;
  let previousDecision = null;
  let previousRequest = null;
  let seenReactions = new Set();
  let reactionTimer = null;
  let foldTimer = null;
  let punchTimer = null;
  let elapsedFloor = null;
  let hydrated = false;
  let renderedPhase = null;

  const finite = value => typeof value === 'number' && Number.isFinite(value) && value >= 0;
  const text = (value, fallback = '', max = 100) => typeof value === 'string' && value.trim() ? value.trim().slice(0, max) : fallback;
  function stateOf(s) {
    const supplied = validStates.has(s.state) ? s.state : 'UNAVAILABLE';
    const now = Date.now();
    const generated = finite(s.generated_at) ? s.generated_at * 1000 : receivedAt;
    if (now - receivedAt > FRESH_MS || now - generated > FRESH_MS || generated > now + FRESH_MS) return 'UNAVAILABLE';
    return supplied;
  }
  function phaseOf(s, state) {
    return state === 'PLAYING' ? (activePhases.has(s.phase) ? s.phase : 'observing') : state.toLowerCase();
  }
  function stopReaction(clearFold = false) {
    clearTimeout(reactionTimer);
    clearTimeout(foldTimer);
    $('reaction').hidden = true;
    $('reaction').className = 'reaction';
    if (clearFold) $('folded-reaction').hidden = true;
  }
  function resetAnimations() {
    clearTimeout(punchTimer);
    card.classList.remove('punch');
    stopReaction(true);
  }
  function punch() {
    clearTimeout(punchTimer);
    card.classList.remove('punch');
    void card.offsetWidth;
    card.classList.add('punch');
    punchTimer = setTimeout(() => card.classList.remove('punch'), 380);
  }
  function showReaction(r) {
    if (!r || !text(r.id) || seenReactions.has(r.id)) return;
    seenReactions.add(r.id);
    // Bounded memory; IDs are monotonic source events and historical hydration is suppressed.
    if (seenReactions.size > 256) seenReactions.delete(seenReactions.values().next().value);
    if (!['confusion', 'kill', 'level', 'recovery'].includes(r.kind)) return;
    if (!finite(r.at) || Math.abs(Date.now() / 1000 - r.at) > FRESH_MS / 1000) return;
    stopReaction(true);
    const icons = {confusion: '🔎', kill: '⚔️', level: '⚡', recovery: '🔄'};
    const defaults = {confusion: 'LOOKING FOR A TARGET', kill: 'KILL CONFIRMED', level: 'LEVEL UP!', recovery: 'BACK IN THE GAME'};
    const caption = text(r.caption, defaults[r.kind], 60);
    $('reaction-icon').textContent = icons[r.kind];
    $('reaction-caption').textContent = caption;
    $('reaction-note').textContent = r.late ? 'Earlier event, just confirmed' : '';
    $('reaction').className = `reaction ${r.kind} pop`;
    $('reaction').hidden = false;
    reactionTimer = setTimeout(() => {
      $('reaction').classList.add('folding');
      $('folded-reaction').textContent = icons[r.kind];
      $('folded-reaction').title = caption;
      $('folded-reaction').hidden = false;
      foldTimer = setTimeout(() => { $('reaction').hidden = true; }, 170);
    }, REACTION_MS);
  }
  function renderTimer(s, phase) {
    if (phase === 'thinking') {
      if (finite(s.request_elapsed)) {
        const elapsed = s.request_elapsed + Math.max(0, performance.now() - s._receivedMono) / 1000;
        elapsedFloor = Math.max(elapsedFloor || 0, elapsed);
        $('timer-value').textContent = elapsedFloor.toFixed(2) + 's';
        $('timer-label').textContent = 'request pending';
      } else {
        $('timer-value').textContent = '…';
        $('timer-label').textContent = 'request pending · time unknown';
      }
    } else if (activePhases.has(phase) && finite(s.response_ms)) {
      $('timer-value').textContent = (s.response_ms / 1000).toFixed(2) + 's';
      $('timer-label').textContent = 'Sage response';
    } else {
      $('timer-value').textContent = '—';
      $('timer-label').textContent = phase === 'paused' ? 'Waiting to resume' : phase === 'stopped' ? 'Session ended' : phase === 'blocked' ? 'Waiting for recovery' : activePhases.has(phase) ? 'No response timing yet' : 'Live timing unavailable';
    }
  }
  function render() {
    if (!snapshot) return;
    const s = snapshot;
    const state = stateOf(s);
    const phase = phaseOf(s, state);
    const idle = state !== 'PLAYING';
    if (phase !== renderedPhase) {
      hud.className = `phase-${phase}${idle ? ' is-idle' : ''}`;
      if (idle) resetAnimations();
      renderedPhase = phase;
    }
    const labels = {
      thinking: ['🧠', 'SAGE / THE BRAIN', 'CHOOSING…', 'THINKING', 'Sage request pending'],
      decision: ['🧠', s.choice_kind === 'observation' ? 'SAGE ASSESSED' : 'SAGE CHOSE', text(s.choice_label, 'CHOICE RECEIVED'), s.choice_kind === 'observation' ? 'ASSESSED' : 'DECIDED', s.choice_kind === 'observation' ? 'Assessment received' : 'Choice received'],
      executing: ['✋', 'HARNESS / THE HANDS', text(s.execution_label, 'EXECUTING…'), 'EXECUTING', 'Following the decision'],
      verifying: ['👀', 'HARNESS / THE EYES', 'CHECKING RESULT', 'VERIFYING', 'Outcome pending'],
      observing: ['👀', 'HARNESS / THE EYES', 'READING THE SCENE', 'OBSERVING', 'Watching the game'],
      paused: ['Ⅱ', 'SESSION PAUSED', s.reason === 'clean_focus_lost' ? 'WOW NOT IN FOREGROUND' : s.reason === 'operator_pause' ? 'PAUSED BY OPERATOR' : 'INPUT PAUSED', 'PAUSED', s.reason === 'clean_focus_lost' ? 'Waiting for WoW foreground' : 'Waiting to resume'],
      stopped: ['■', 'SESSION STOPPED', 'THAT’S A WRAP', 'STOPPED', 'Input stopped'],
      blocked: ['⏸', 'HARNESS / RECOVERY', ['blocked_travel_choices', 'navigation_no_action_selected'].includes(s.reason) ? 'NAVIGATION STALLED' : 'RECOVERY NEEDED', s.reason === 'navigation_no_action_selected' ? 'WAITING' : 'BLOCKED', s.reason === 'navigation_no_action_selected' ? 'No hunting action selected' : s.reason === 'blocked_travel_choices' ? 'Internal navigation choices exhausted' : 'Harness recovery is unresolved'],
      stale: ['◌', 'LIVE FEED', 'FEED DELAYED', 'UNAVAILABLE', 'Waiting for a fresh update'],
      unavailable: ['◌', 'LIVE FEED', 'WAITING FOR SAGE', 'UNAVAILABLE', 'Feed unavailable']
    };
    const [emoji, actor, title, stateLabel, note] = labels[phase];
    $('actor-emoji').textContent = emoji;
    $('actor-label').textContent = actor;
    $('decision-title').textContent = title.toUpperCase();
    $('decision-title').classList.toggle('long', title.length > 21);
    $('state-label').textContent = stateLabel;
    $('status-note').textContent = note;
    $('last-choice').textContent = text(s.choice_label, 'Waiting for a decision');
    $('choice-prefix').textContent = s.choice_kind === 'observation' ? (phase === 'decision' ? 'ASSESSMENT' : 'LAST ASSESSMENT') : (phase === 'decision' ? 'SAGE CHOSE' : 'LAST CHOICE');
    const current = Number.isInteger(s.current_level) && s.current_level > 0 ? s.current_level : '?';
    const goal = Number.isInteger(s.goal_level) && s.goal_level > 0 ? s.goal_level : '?';
    $('level-label').textContent = `LVL ${current} → ${goal}`;
    const activeActor = idle ? null : phase === 'thinking' || phase === 'decision' ? 'sage' : phase === 'executing' ? 'harness' : 'eyes';
    document.querySelectorAll('[data-actor]').forEach(el => el.classList.toggle('active', el.dataset.actor === activeActor));
    renderTimer(s, phase);
  }
  window.updateSageHUD = value => {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return;
    const nextKey = JSON.stringify([value.session_id || null, value.source || null, value.source_revision || null]);
    const sourceChanged = nextKey !== sourceKey;
    if (sourceChanged) {
      sourceKey = nextKey;
      seenReactions = new Set();
      previousDecision = null;
      previousRequest = null;
      elapsedFloor = null;
      hydrated = false;
      resetAnimations();
    }
    if (value.request_id !== previousRequest) elapsedFloor = null;
    previousRequest = value.request_id;
    receivedAt = Date.now();
    snapshot = {...value, _receivedMono: performance.now()};
    const state = stateOf(snapshot);
    const changedDecision = value.decision_id && value.decision_id !== previousDecision;
    render();
    if (state === 'PLAYING' && hydrated) {
      // A short phase can be missed between polls; the actual event ID still earns its punch.
      const decisionFresh = finite(value.decision_at) && Math.abs(Date.now() / 1000 - value.decision_at) <= FRESH_MS / 1000;
      if (changedDecision && decisionFresh) punch();
      showReaction(value.reaction);
    } else if (value.reaction && value.reaction.id) {
      seenReactions.add(value.reaction.id);
    }
    previousDecision = value.decision_id;
    hydrated = true;
  };
  function resize() {
    const scale = Math.max(.55, Math.min(1, (innerWidth - 36) / 444, innerHeight / 967));
    hud.style.setProperty('--hud-scale', String(scale));
  }
  window.addEventListener('resize', resize);
  resize();
  // Only interpolation of a real pending request and feed expiry; never advances game phases.
  setInterval(render, 100);
})();
