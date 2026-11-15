/**
 * Name an application asset URL without changing its signed destination.
 * @param href - A safe absolute link destination.
 * @param fileLabel - Chat-owned generic file label for opaque asset URLs.
 * @returns A short name, or undefined for unrelated links.
 */
export function assetLinkLabel(href: string, fileLabel: string): string | undefined {
  let url: URL
  try { url = new URL(href) } catch { return undefined }
  const local = ['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)
  if (url.protocol !== 'https:' && url.protocol !== 'http:') return undefined
  if (url.username || url.password) return undefined
  if (url.hostname !== 'banana.wuyullm.com' && !local) return undefined
  if (url.pathname !== '/ppt-agent/assets' && !(local && url.pathname === '/assets')) return undefined
  return url.searchParams.get('token') ? fileLabel : undefined
}
