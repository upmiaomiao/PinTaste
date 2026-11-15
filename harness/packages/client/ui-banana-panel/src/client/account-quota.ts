/** Cookie-authenticated account views; browser callers never copy credentials. */
export interface QuotaBalance { granted: number; available: number; reserved: number; spent: number }
export interface AccountQuota {
  unlimited?: boolean
  tariffs: { image: { points: number }; trace: { points: number }; text: { input_tokens_per_point: number; output_tokens_per_point: number }; search: { points: number }; fetch: { points: number } }
  control: { spending_frozen: boolean }
  user_id: string; day: string; timezone: string; reset_at: string
  daily: { pages: QuotaBalance; points: QuotaBalance }
  bonus: { pages: QuotaBalance; points: QuotaBalance }
  available: { pages: number; points: number }
  reserved: { pages: number; points: number }
}
export interface AccountView { username: string; admin: boolean; quota: AccountQuota }
export interface QuotaEntry {
  id: string; event: string; kind: string | null; day: string; pages: number; points: number
  charged_pages: number; charged_points: number; reason: string; created_at: string
}
export class AccountReadError extends Error {
  constructor(readonly status: number) { super('ACCOUNT_UNAVAILABLE') }
}
async function get(path: string, signal?: AbortSignal): Promise<unknown> {
  const response = await fetch('/agent-api' + path, { credentials: 'same-origin', cache: 'no-store', ...(signal ? { signal } : {}) })
  const value: unknown = await response.json().catch(() => null)
  if (!response.ok || !value || typeof value !== 'object' || !('success' in value) || value.success !== true || !('data' in value)) throw new AccountReadError(response.status)
  return value.data
}
const record = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value)
const natural = (value: unknown): value is number => Number.isSafeInteger(value) && Number(value) >= 0
function balance(value: unknown): value is QuotaBalance { return record(value) && ['granted', 'available', 'reserved', 'spent'].every(key => natural(value[key])) }
function quota(value: unknown): value is AccountQuota {
  return record(value) && typeof value.user_id === 'string' && typeof value.day === 'string' && typeof value.timezone === 'string' && typeof value.reset_at === 'string'
    && record(value.control) && typeof value.control.spending_frozen === 'boolean'
    && record(value.tariffs) && ['image', 'trace', 'search', 'fetch'].every(key => record(value.tariffs) && record(value.tariffs[key]) && natural(value.tariffs[key].points))
    && record(value.tariffs.text) && natural(value.tariffs.text.input_tokens_per_point) && natural(value.tariffs.text.output_tokens_per_point)
    && ['daily', 'bonus'].every(key => record(value[key]) && balance(value[key].pages) && balance(value[key].points))
    && ['available', 'reserved'].every(key => record(value[key]) && natural(value[key].pages) && natural(value[key].points))
}
/** Read server-verified identity and quota together, rejecting mismatched users. */
export async function readAccount(signal?: AbortSignal): Promise<AccountView> {
  const [user, view] = await Promise.all([get('/api/auth/me', signal), get('/api/auth/quota', signal)])
  if (!record(user) || typeof user.username !== 'string' || !quota(view) || user.id !== view.user_id) throw new AccountReadError(502)
  return { username: user.username, admin: user.role === 'admin', quota: view }
}
/** The redemption failed; message is the work service's own Chinese reason. */
export class RedeemError extends Error {
  constructor(readonly code: string, message: string) { super(message) }
}
/** Redeem a one-use code into long-term points for the signed-in user. */
export async function redeemQuotaCode(code: string): Promise<{ points: number }> {
  const response = await fetch('/agent-api/api/auth/quota/redeem', {
    method: 'POST', credentials: 'same-origin', cache: 'no-store', redirect: 'error',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code }),
  })
  const value: unknown = await response.json().catch(() => null)
  if (!response.ok || !record(value) || value.success !== true || !record(value.data) || !natural(value.data.points)) {
    const error = record(value) && record(value.error) ? value.error : {}
    throw new RedeemError(typeof error.code === 'string' ? error.code : 'REDEEM_FAILED', typeof error.message === 'string' ? error.message : '')
  }
  return { points: value.data.points }
}
/** Read only the signed-in user's bounded recent ledger. */
export async function readQuotaLedger(signal?: AbortSignal): Promise<QuotaEntry[]> {
  const value = await get('/api/auth/quota/ledger?limit=10&offset=0', signal)
  if (!record(value) || !Array.isArray(value.entries) || value.entries.length > 10 || value.entries.some(entry => !record(entry)
    || !['id', 'event', 'day', 'reason', 'created_at'].every(key => typeof entry[key] === 'string') || (entry.kind !== null && typeof entry.kind !== 'string')
    || !['pages', 'points', 'charged_pages', 'charged_points'].every(key => Number.isSafeInteger(entry[key])))) throw new AccountReadError(502)
  return value.entries as unknown as QuotaEntry[]
}
