// @vitest-environment node
import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { PresentationPreferencesController } from '../src/client/presentation-preferences.ts'
import { defaultPresentationConfig, parsePresentationConfig, type PresentationConfig, type PresentationConfigRecord } from '../src/presentation-config.ts'
import { zh, en, type BananaPanelKey } from '../src/client/locales.ts'

const expected = JSON.parse(readFileSync(new URL('./expected/task-entry-submit.json', import.meta.url), 'utf8')) as Record<string,string>
const translate = (dictionary: typeof zh | typeof en) => (key: BananaPanelKey, values: Record<string,string> = {}) => dictionary[key].replace(/\{(\w+)\}/g, (_, name: string) => values[name] ?? '{' + name + '}')
function fixture() {
  let saved: PresentationConfigRecord = {revision:0,config:defaultPresentationConfig()}
  const create = () => new PresentationPreferencesController(async request => {
    if (request.operation === 'save') saved={revision:saved.revision+1,config:request.config as PresentationConfig}
    return structuredClone(saved)
  })
  return {create,read:()=>saved}
}

describe('shared PPT task entry', () => {
  it('saves compatible presets and restores them in a fresh controller', async () => {
    const f=fixture(),first=f.create();await first.load();await first.chooseTask('create','pages')
    expect(f.read().config).toMatchObject({task_mode:'create',creation_kind:'pages',page_count:2})
    await first.chooseTask('create','asset')
    const second=f.create();await second.load()
    expect(second.selected()).toMatchObject({task_mode:'create',creation_kind:'asset',page_count:1})
    first.dispose();second.dispose()
  })
  it('pins logged text in both UI languages without starting a model', async () => {
    const f=fixture(),c=f.create();await c.load();await c.chooseTask('create','asset')
    expect(await c.submission('ENTRY71_TEST_NO_MODEL',translate(zh))).toBe(expected.asset_zh)
    expect(await c.submission('ENTRY71_TEST_NO_MODEL',translate(en))).toBe(expected.asset_en)
    c.dispose()
  })
  it('uses a bound legacy project mode without rewriting future defaults', async () => {
    const f=fixture(),c=f.create();await c.load();c.bindProject('owned-original','relayout')
    expect((await c.submission('继续重排',translate(zh)))).toContain('PPT任务：重排已有PPT')
    expect(f.read().revision).toBe(0)
    await c.load();expect(c.effectiveTaskMode()).toBe('relayout');c.dispose()
  })
  it('normalizes legacy records and rejects unknown task choices', () => {
    expect(parsePresentationConfig({page_count:null,language:'zh',output_preference:'editable'})).toMatchObject({task_mode:'create',creation_kind:'deck'})
    expect(()=>parsePresentationConfig({...defaultPresentationConfig(),task_mode:'unsupported'})).toThrow('PPT_CONFIG_INVALID')
  })
})
