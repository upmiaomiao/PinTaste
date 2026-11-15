const finite = value => typeof value === 'number' && Number.isFinite(value) && value >= 0;
/** Only server-derived job phases receive a client monotonic live increment. */
export function elapsedTiming(turn, elapsed = 0) {
  const value = turn?.executionTiming;
  if (!value || typeof value !== 'object' || !Number.isFinite(value.sampled_at)) return null;
  const active = ['queued', 'running'].includes(turn.status) && value.phase === turn.status;
  const extra = active && finite(elapsed) ? elapsed : 0;
  const result = {};
  for (const key of ['queue_seconds', 'execution_seconds', 'total_seconds']) {
    if (!finite(value[key])) continue;
    const ticking = key === 'total_seconds' || key === 'queue_seconds' && turn.status === 'queued' || key === 'execution_seconds' && turn.status === 'running';
    result[key] = value[key] + (ticking ? extra : 0);
  }
  return Object.keys(result).length ? result : null;
}
/** Group only exact recorded reasoning; old or inconsistent metadata falls back losslessly. */
export function reasoningSections(turn) {
  const text = typeof turn?.reasoning === 'string' ? turn.reasoning : '';
  if (!text) return [];
  const rounds = turn.reasoningRounds;
  if (!Array.isArray(rounds) || !rounds.length || rounds.some(item => !item || typeof item.text !== 'string'
      || item.round !== null && (!Number.isInteger(item.round) || item.round < 1))
      || rounds.map(item => item.text).join('') !== text) return [{ round: null, text }];
  return rounds.map(item => ({ round: item.round, text: item.text }));
}
