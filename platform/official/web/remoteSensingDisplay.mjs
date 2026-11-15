export function remoteSensingDisplayTurn(turn) {
  // Server-derived visualFiles includes only the assets actually sent this turn,
  // including an inherited follow-up. Never guess from earlier chat messages.
  const images = Array.isArray(turn.visualFiles) ? turn.visualFiles
    : (turn.files || []).filter(file => file.kind === 'image');
  return {...turn, annotationImages: images,
    annotationImageIndexBase: turn.remoteSensingImageIndexBase === 1 ? 1 : undefined};
}

export const remoteSensingIsGenerating = turn => ['queued','running','streaming'].includes(turn.status);
export const remoteSensingHasMarkup = text => /(?:<(?:\/?[A-Za-z_][\w:.-]*(?:\s|>|$)|\/?$)|(?:^|\n)\s*[`~]{3,}\s*xml\b)/i.test(String(text || ''));
