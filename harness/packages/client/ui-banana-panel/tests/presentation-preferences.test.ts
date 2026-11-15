// @vitest-environment node
import { describe, expect, it } from 'vitest'
import { PresentationPreferencesController, spokenPreferences } from '../src/client/presentation-preferences.ts'
import { defaultPresentationConfig, type PresentationConfig, type PresentationConfigRecord } from '../src/presentation-config.ts'
import { zh, type BananaPanelKey } from '../src/client/locales.ts'

const translate = (key: BananaPanelKey, params: Record<string, string> = {}) => Object.entries(params).reduce((s, [k, v]) => s.replaceAll('{' + k + '}', v), zh[key])
function fixture() {
  let record: PresentationConfigRecord = { revision: 0, config: defaultPresentationConfig() }
  const requests: Record<string, unknown>[] = []
  const transport = async (request: Record<string, unknown>) => {
    requests.push(request)
    if (request.operation === 'save') record = { revision: record.revision + 1, config: request.config as PresentationConfig }
    return structuredClone(record)
  }
  return { requests, create: () => new PresentationPreferencesController(transport) }
}

describe('saved configuration separate from generation', () => {
  it('restores saved preferences into a fresh composer', async () => {
    const f = fixture(), first = f.create()
    await first.load()
    first.edit({ fixed: true, countDraft: '1', output: 'image', language: 'en' })
    await first.save()
    const next = f.create(); await next.load()
    expect(next.selected()).toEqual({ page_count: 1, output_preference: 'image', language: 'en' })
    expect(f.requests.map(r => r.operation)).toEqual(['read', 'save', 'read'])
    expect(await next.submission('生成PPT', translate)).toMatchInlineSnapshot('"生成PPT\n\n【本次PPT设置】【PPT页数：1页（单页PPT，不额外增加封面）】；输出：图片版 PPT；内容语言：英文。以上为本对话偏好，本条正文明确要求优先；仅用于新建或用户明确要求修改的项目，不自动覆盖已有项目，不代表模板、草稿、页图或付费操作已获确认。"')
  })
  it('updates the same saved preferences from explicit chat changes', async () => {
    const f = fixture(), controller = f.create(); await controller.load()
    const message = await controller.submission('改成二十页，用英文，输出图片版', translate)
    expect(controller.selected()).toEqual({ page_count: 20, output_preference: 'image', language: 'en' })
    expect(message).toContain('20页')
    expect(f.requests.filter(r => r.operation === 'save')).toHaveLength(1)
  })
  it('keeps one-off overrides out of conversation defaults', async () => {
    const f = fixture(), controller = f.create(); await controller.load()
    controller.edit({ fixed: true, countDraft: '20' }); await controller.save()
    expect(await controller.submission('这次临时做5页', translate)).toContain('5页')
    expect(controller.selected().page_count).toBe(20)
    expect(f.requests.filter(r => r.operation === 'save')).toHaveLength(1)
  })
  it.each(['0', '81', '2.5', '', '2e1', '05'])('blocks invalid drafts before any save or downstream action: %j', async countDraft => {
    const f = fixture(), controller = f.create(); await controller.load()
    controller.edit({ fixed: true, countDraft })
    await expect(controller.submission('开始', translate)).rejects.toThrow('PPT_PAGE_COUNT_INVALID')
    expect(f.requests).toHaveLength(1)
  })
  it('preserves the draft and blocks sending when a save fails', async () => {
    const controller = new PresentationPreferencesController(async request => {
      if (request.operation === 'save') throw new Error('PPT_CONFIG_UNAVAILABLE')
      return { revision: 0, config: defaultPresentationConfig() }
    })
    await controller.load(); controller.edit({ fixed: true, countDraft: '10' })
    await expect(controller.save()).rejects.toThrow()
    expect(controller.state.getSnapshot().countDraft).toBe('10')
    await expect(controller.submission('开始', translate)).rejects.toThrow('PPT_CONFIG_NOT_READY')
  })
  it('requires reloading after a stale revision conflict', async () => {
    const controller = new PresentationPreferencesController(async request => {
      if (request.operation === 'save') throw new Error('PPT_CONFIG_CONFLICT')
      return { revision: 2, config: { ...defaultPresentationConfig(), page_count: 15 } }
    })
    await controller.load(); controller.edit({ countDraft: '10' })
    await expect(controller.save()).rejects.toThrow('PPT_CONFIG_CONFLICT')
    await expect(controller.submission('继续', translate)).rejects.toThrow('PPT_CONFIG_NOT_READY')
    await controller.load(); expect(controller.selected().page_count).toBe(15)
  })
  it.each(['比如做20页', '不要做20页', '改成20页还是10页？', '原文说“做20页”', '> 改成20页'])('does not save quoted, negative or uncertain text: %s', text => {
    expect(spokenPreferences(text)).toEqual({})
  })
  it('rejects an out-of-range spoken page count rather than silently falling back', () => {
    expect(() => spokenPreferences('改成100页')).toThrow('PPT_PAGE_COUNT_INVALID')
  })
})
