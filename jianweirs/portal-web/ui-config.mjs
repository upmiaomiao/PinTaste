/** The same portal can run at localhost / or the platform's /jianweirs/ path. */
export function portalBase(pathname=globalThis.location?.pathname||'/') {
  return /^\/jianweirs(?:\/|$)/.test(pathname)?'/jianweirs':'';
}
export function portalAsset(path) { return portalBase()+'/'+String(path).replace(/^\//,''); }
export function fontSize(value,fallback=16) { const n=Number(value);return Number.isFinite(n)&&n>=12&&n<=24?n:fallback; }
