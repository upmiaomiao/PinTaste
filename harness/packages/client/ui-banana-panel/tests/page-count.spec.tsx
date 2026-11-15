// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { PageCountController, selectedPageCount } from '../src/client/page-count.ts'
import { PageCountControl } from '../src/client/PageCountControl.tsx'
import { zh } from '../src/client/locales.ts'

const summary = (count: number) => count === 1 ? zh['pageCount.singleSummary'] : `【整套PPT页数：${count}页（含封面）】`

describe('full-deck count in the work composer', () => {
  it('keeps written requirements untouched until an exact count is selected', () => {
    const controller = new PageCountController()
    expect(controller.submission('做一份五页方案', summary)).toBe('做一份五页方案')
    controller.choose(true)
    controller.edit('5')
    expect(controller.submission('做一份方案', summary)).toBe('做一份方案\n\n' + summary(5))
    controller.choose(false)
    expect(controller.submission('改成按材料安排', summary)).toBe('改成按材料安排')
  })
  it.each(['', '0', '81', '2.5', '2e1', '-5', ' 5', '05'])('blocks an invalid exact draft: %j', draft => {
    const controller = new PageCountController()
    controller.choose(true)
    controller.edit(draft)
    expect(() => controller.submission('测试', summary)).toThrow('PPT_PAGE_COUNT_INVALID')
  })
  it.each([1, 2, 5, 80])('submits the complete selected integer %i', count => {
    expect(selectedPageCount({ fixed: true, draft: String(count) })).toBe(count)
  })
  it('requests one content slide without implying a one-page full deck or extra cover', () => {
    const controller = new PageCountController()
    controller.choose(true)
    controller.edit('1')
    const text = controller.submission('生成一页PPT', summary)
    expect(text).toContain('【PPT页数：1页（单页PPT，不额外增加封面）】')
    expect(text).not.toContain('整套PPT页数：1')
  })
  it('does not duplicate a selection on transport retry or share it with another Session', () => {
    const first = new PageCountController(), second = new PageCountController()
    first.choose(true)
    first.edit('20')
    const text = first.submission('需求', summary)
    expect(first.submission(text, summary)).toBe(text)
    expect(second.submission('需求', summary)).toBe('需求')
  })
  it('renders the exact count and accessible validation in a real component', () => {
    const component = PageCountControl as unknown as (props: Record<string, unknown>) => ReturnType<typeof createElement>
    const html = renderToStaticMarkup(createElement(component, {
      sessionId: 'work-test',
      useSessions: (select: (s: unknown) => unknown) => select({ byId: { 'work-test': { projectionValues: { agentPreset: 'wuyu-work' } } } }),
      useInput: (select: (s: unknown) => unknown) => select({ phase: 'plain' }),
      usePageCount: (select: (s: unknown) => unknown) => select({ fixed: true, draft: '20' }),
      choosePageCount: () => {}, editPageCount: () => {},
      t: (key: keyof typeof zh) => zh[key],
    }))
    expect(html).toContain('PPT 页数')
    expect(html).toContain('value="20"')
    expect(html).toContain('min="1"')
    expect(html).toContain('max="80"')
    expect(html).toContain('aria-invalid="false"')
  })
})
