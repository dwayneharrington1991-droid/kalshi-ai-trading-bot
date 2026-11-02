/* Status only: existing forecasts remain experimental and cannot authorize orders. */
(() => {
  const show = (id, value) => { const node = document.getElementById(id); if (node) node.textContent = value; };
  function update() {
    const state = typeof lastState === 'undefined' ? null : lastState;
    const disconnected = typeof lastSuccess === 'undefined' || Date.now() - lastSuccess > 5000;
    const blocked = !state || disconnected || state.stale || !((state.remaining_seconds - (Date.now() - lastSuccess) / 1000) > 0);
    const opening = state?.prediction_engine?.initial;
    const sameContract = opening && (!opening.ticker || opening.ticker === state?.market?.ticker);
    show('opening-ai-status', !blocked && sameContract && Number.isFinite(opening?.up)
      ? 'EXPERIMENTAL · UNVALIDATED · NO VERIFIED EDGE' : 'WAIT · OPENING UNAVAILABLE OR STALE');
    show('scalp-ai-status', blocked ? 'WAIT · DATA UNAVAILABLE OR STALE' : 'EXPERIMENTAL · UNVALIDATED · NO VERIFIED EDGE');
    show('combined-ai-status', 'WAIT · NO VERIFIED EDGE');
    show('combined-ai-reason', state?.combined_ai?.reason || 'No dedicated trained Opening + Scalping ensemble is loaded.');
  }
  update();
  setInterval(update, 1000);
})();
