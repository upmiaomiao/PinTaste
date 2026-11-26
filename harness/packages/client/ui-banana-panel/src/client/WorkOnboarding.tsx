/** op201: first-run questionnaire + three-step guide on the empty Work page.
 * Answers live in the work service: GET/PUT /agent-api/api/design-memory/profile (fixed codes only).
 * lib/client.js carries the compiled equivalent (operations/op201-onboarding/patch_banana_201.py);
 * keep both in step when changing this file. */
import { createElement, Fragment, useEffect, useRef, useState } from 'react'

type Pair = readonly [string, string]
type Answers = { industries: string[]; role: string | null; deck_types: string[] }
type Question = { key: keyof Answers; multi: boolean; title: Pair; hint: Pair; options: readonly (readonly [string, Pair])[] }
type ProfileState = { available: boolean; profile: (Answers & { status: 'done' | 'skipped' }) | null }

// Codes must match backend services/wuyu_user_profile.py and services/wuyu_deck_types.py.
const QUESTIONS: readonly Question[] = [
  { key: 'industries', multi: true, title: ['你主要在哪个领域工作？', 'Which field do you work in?'], hint: ['最多选 3 个。', 'Pick up to 3.'], options: [
    ['solid_waste', ['固废与资源循环', 'Solid waste & recycling']], ['environment', ['生态环境', 'Environment']], ['energy', ['能源化工', 'Energy & chemicals']],
    ['engineering', ['工程建设', 'Engineering & construction']], ['research', ['科研院所', 'Research institute']], ['education', ['高校教育', 'Higher education']],
    ['government', ['政府监管', 'Government & regulation']], ['enterprise', ['企业经营', 'Business']], ['other', ['其他', 'Other']]] },
  { key: 'role', multi: false, title: ['你平时主要做什么？', 'What do you mostly do?'], hint: ['选一个最接近的。', 'Pick the closest one.'], options: [
    ['research', ['做研究、写论文', 'Research & papers']], ['student', ['在读学生', 'Student']], ['engineering', ['技术与工程', 'Engineering']],
    ['management', ['管理与汇报', 'Management & reporting']], ['consulting', ['咨询与方案', 'Consulting & proposals']], ['teaching', ['教学与培训', 'Teaching & training']], ['other', ['其他', 'Other']]] },
  { key: 'deck_types', multi: true, title: ['你常做哪类 PPT？', 'Which presentations do you make most?'], hint: ['最多选 3 个。需求里没写类型时，会按第一个推荐模板。', 'Pick up to 3. When a request names no type, templates for the first one are suggested.'], options: [
    ['proposal_defense', ['开题答辩', 'Proposal defense']], ['thesis_defense', ['毕业论文答辩', 'Thesis defense']], ['master_defense', ['硕士论文答辩', 'Master defense']],
    ['doctoral_defense', ['博士论文答辩', 'PhD defense']], ['qualifying_exam', ['资格考核', 'Qualifying exam']], ['academic_report', ['学术汇报 / 组会', 'Academic talk']],
    ['grant', ['项目申请', 'Grant application']], ['award', ['成果报奖', 'Award application']], ['technical_proposal', ['技术方案', 'Technical proposal']],
    ['work_report', ['工作汇报', 'Work report']], ['training', ['培训课件', 'Training']], ['company_intro', ['企业介绍', 'Company profile']], ['business_plan', ['商业计划', 'Business plan']]] },
]
const GUIDE: readonly (readonly [Pair, Pair])[] = [
  [['说清楚要做什么', 'Say what you need'], ['在下面的输入框描述主题、汇报对象和时长，或点一张任务卡开始。', 'Describe the topic, audience and length below, or start from a task card.']],
  [['上传材料、选模板', 'Add materials, pick a template'], ['把论文、报告、数据或旧 PPT 拖进来；在右侧面板选一套模板，或上传自己单位的模板。', 'Drop in papers, reports, data or an old deck; choose a template in the side panel or upload your own.']],
  [['确认后生成，逐页修改', 'Confirm, generate, refine'], ['先确认大纲和每页内容再出图；任何一页都可以单独提修改意见。', 'Approve the outline and pages before images are made; any page can be revised on its own.']],
]
const COPY = {
  welcome: ['欢迎使用！先花 10 秒了解你', 'Welcome! 10 seconds to get to know you'], step: ['第 {n} / 3 步', 'Step {n} of 3'], picked: ['已选 {n}/3', '{n}/3 selected'],
  later: ['以后再说', 'Later'], back: ['上一步', 'Back'], skip: ['跳过此题', 'Skip'], next: ['下一步', 'Next'], finish: ['完成', 'Finish'], close: ['关闭', 'Close'],
  saved: ['已记下你的偏好', 'Preferences saved'], guide: ['三步做出一份 PPT', 'Three steps to a presentation'], guideNote: ['回答随时可以在这里的「调整使用偏好」里修改。', 'You can change your answers any time with “Edit my preferences” on this page.'],
  self: ['我先自己看看', 'Let me look around'], start: ['从整套 PPT 开始', 'Start a full deck'], failed: ['没有保存成功，可稍后点「调整使用偏好」重新填写。', 'Not saved. You can fill it in later with “Edit my preferences”.'],
  edit: ['调整使用偏好', 'Edit my preferences'], fill: ['填写使用偏好', 'Tell us about your work'], help: ['使用指引', 'How it works'],
} as const satisfies Record<string, Pair>
const URL = '/agent-api/api/design-memory/profile'
const UNAVAILABLE: ProfileState = { available: false, profile: null }
let request: Promise<ProfileState> | null = null
let autoShown = false

function load(): Promise<ProfileState> {
  request ??= fetch(URL, { credentials: 'same-origin', cache: 'no-store', redirect: 'error' })
    .then(response => response.ok ? response.json() : null)
    .then(body => body?.success === true && body.data && typeof body.data === 'object' ? body.data as ProfileState : UNAVAILABLE)
    .catch(() => UNAVAILABLE)
  return request
}

function payload(answers: Answers, skipped: boolean) {
  if (skipped) return { status: 'skipped' }
  const out: Record<string, unknown> = { status: 'done', industries: answers.industries.slice(0, 3), deck_types: answers.deck_types.slice(0, 3) }
  if (answers.role) out.role = answers.role
  return answers.industries.length || answers.deck_types.length || answers.role ? out : { status: 'skipped' }
}

/** Links + the dialog. Shown automatically once per page load while the owner has never answered or skipped. */
export function WorkOnboarding({ en, onStart }: { en: boolean; onStart: () => void }) {
  const L = (pair: Pair, values?: Record<string, number>) => Object.entries(values ?? {}).reduce((text, [k, v]) => text.replace('{' + k + '}', String(v)), pair[en ? 1 : 0])
  const empty: Answers = { industries: [], role: null, deck_types: [] }
  const [data, setData] = useState<ProfileState | null>(null), [open, setOpen] = useState(false), [manual, setManual] = useState(false)
  const [step, setStep] = useState(0), [answers, setAnswers] = useState<Answers>(empty), [saving, setSaving] = useState(false), [failed, setFailed] = useState(false)
  const dialog = useRef<HTMLDialogElement>(null)
  useEffect(() => {
    let live = true
    void load().then(loaded => {
      if (!live) return
      setData(loaded)
      if (loaded.available && loaded.profile === null && !autoShown) { autoShown = true; setManual(false); setStep(0); setAnswers(empty); setOpen(true) }
    })
    return () => { live = false }
  }, [])
  useEffect(() => {
    const element = dialog.current
    if (!element || typeof element.showModal !== 'function') return
    if (open && !element.open) element.showModal()
    if (!open && element.open) element.close()
  }, [open])
  useEffect(() => { if (open) dialog.current?.querySelector<HTMLElement>('[data-op201-focus]')?.focus() }, [open, step])
  if (!data?.available) return null

  const save = async (skipped: boolean) => {
    setSaving(true); setFailed(false)
    let ok = false
    try {
      const response = await fetch(URL, { method: 'PUT', credentials: 'same-origin', cache: 'no-store', redirect: 'error', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload(answers, skipped)) })
      const body = response.ok ? await response.json() : null
      if (body?.success === true && body.data) { request = Promise.resolve(body.data as ProfileState); setData(body.data as ProfileState); ok = true }
    } catch { /* reported below */ }
    setSaving(false); if (!ok) setFailed(true)
    return ok
  }
  const close = () => { setOpen(false); setManual(false) }
  const later = async () => { if (saving) return; if (!manual) await save(false); close() }
  const next = async () => {
    if (saving) return
    if (step < QUESTIONS.length - 1) { setStep(step + 1); return }
    const ok = await save(false)
    if (manual && ok) { close(); return }
    setStep(QUESTIONS.length)
  }
  const reopen = (guideOnly: boolean) => {
    const saved = data.profile
    setAnswers({ industries: saved?.industries ?? [], role: saved?.role ?? null, deck_types: saved?.deck_types ?? [] })
    setManual(true); setFailed(false); setStep(guideOnly ? QUESTIONS.length : 0); setOpen(true)
  }
  const question = QUESTIONS[step]
  const value = question ? answers[question.key] : null
  const count = question ? (question.multi ? (value as string[]).length : value ? 1 : 0) : 0
  const toggle = (code: string) => setAnswers(current => {
    if (!question!.multi) return { ...current, [question!.key]: current[question!.key] === code ? null : code }
    const list = current[question!.key] as string[]
    if (list.includes(code)) return { ...current, [question!.key]: list.filter(item => item !== code) }
    return list.length >= 3 ? current : { ...current, [question!.key]: [...list, code] }
  })
  const body = !open ? null : question ? <>
    <div className="op201-head"><p className="op201-step">{step === 0 && !manual ? L(COPY.welcome) : L(COPY.step, { n: step + 1 })}</p>
      <button type="button" className="op201-x" aria-label={L(COPY.close)} disabled={saving} onClick={() => void later()}>×</button></div>
    <div className="op201-progress" aria-hidden="true">{QUESTIONS.map((_, index) => <i key={index} className={index <= step ? 'on' : ''} />)}</div>
    <h2 id="op201-title">{L(question.title)}</h2>
    <p className="op201-hint">{L(question.hint)}{question.multi ? ' · ' + L(COPY.picked, { n: count }) : ''}</p>
    <div className="op201-options" role="group" aria-labelledby="op201-title">{question.options.map(([code, label], index) => {
      const pressed = question.multi ? (value as string[]).includes(code) : value === code
      return <button type="button" key={code} data-op201-focus={index === 0 ? '' : undefined} aria-pressed={pressed} disabled={saving || question.multi && !pressed && count >= 3} onClick={() => toggle(code)}>{pressed ? '✓ ' : ''}{L(label)}</button>
    })}</div>
    {failed ? <p className="op201-error" role="alert">{L(COPY.failed)}</p> : null}
    <div className="op201-foot">
      {step > 0 ? <button type="button" className="op201-text" disabled={saving} onClick={() => setStep(step - 1)}>{'← ' + L(COPY.back)}</button>
        : <button type="button" className="op201-text" disabled={saving} onClick={() => void later()}>{L(COPY.later)}</button>}
      <span className="op201-actions">
        {count === 0 ? <button type="button" className="op201-text" disabled={saving} onClick={() => void next()}>{L(COPY.skip)}</button> : null}
        {count > 0 ? <button type="button" className="op201-primary" disabled={saving} onClick={() => void next()}>{(step === QUESTIONS.length - 1 ? L(COPY.finish) : L(COPY.next)) + ' →'}</button> : null}
      </span>
    </div>
  </> : <>
    <div className="op201-head"><p className="op201-step">{manual ? L(COPY.guide) : L(COPY.saved)}</p>
      <button type="button" className="op201-x" aria-label={L(COPY.close)} onClick={close}>×</button></div>
    <h2 id="op201-title">{L(COPY.guide)}</h2>
    <ol className="op201-guide">{GUIDE.map(([title, text], index) => <li key={index}><span className="op201-num" aria-hidden="true">{index + 1}</span><div><strong>{L(title)}</strong><p>{L(text)}</p></div></li>)}</ol>
    {failed ? <p className="op201-error" role="alert">{L(COPY.failed)}</p> : null}
    <p className="op201-hint">{L(COPY.guideNote)}</p>
    <div className="op201-foot">
      <button type="button" className="op201-secondary" onClick={close}>{L(COPY.self)}</button>
      <button type="button" className="op201-primary" data-op201-focus="" onClick={() => { close(); onStart() }}>{L(COPY.start) + ' →'}</button>
    </div>
  </>
  return createElement(Fragment, null,
    <div className="op201-links">
      <button type="button" className="op201-link" onClick={() => reopen(false)}>{data.profile?.status === 'done' ? L(COPY.edit) : L(COPY.fill)}</button>
      <button type="button" className="op201-link" onClick={() => reopen(true)}>{L(COPY.help)}</button>
    </div>,
    <dialog ref={dialog} className="op201-dialog" aria-labelledby="op201-title" onCancel={event => { event.preventDefault(); if (question) void later(); else close() }}>{body}</dialog>)
}
// The stylesheet (op201-onboarding.css in lib) is injected by the compiled region; see patch_banana_201.py.
