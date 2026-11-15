// @vitest-environment node
import { describe,expect,it } from 'vitest'
import { readFileSync } from 'node:fs'
import { SearchModeController,armResearchOnServer } from '../src/client/search-mode.ts'

const expected=JSON.parse(readFileSync(new URL('./expected/search-mode-retention.json',import.meta.url),'utf8'))
const settle=()=>new Promise(resolve=>setTimeout(resolve,0))

describe('search selection between user submissions',()=>{
  it('retains web mode across two explicit submissions with separate observations',async()=>{
    const arms:string[]=[],c=new SearchModeController(async mode=>{arms.push(mode)})
    c.toggle('web')
    expect([await c.submission('第一轮制作需求'),await c.submission('第二轮补充需求')]).toEqual(expected.web)
    await settle();expect(arms).toEqual(['web','web']);expect(c.state.getSnapshot().mode).toBe('web')
    c.toggle('web');expect(await c.submission('不联网的需求')).toBe('不联网的需求');expect(arms).toHaveLength(2)
  })
  it('switches to deep mode without changing another Session',async()=>{
    const arms:string[]=[],a=new SearchModeController(async mode=>{arms.push(mode)}),b=new SearchModeController()
    a.toggle('web');a.toggle('deep');expect(await a.submission('深度探索需求')).toBe(expected.deep)
    await settle();expect(arms).toEqual(['deep']);expect(a.state.getSnapshot().mode).toBe('deep');expect(b.state.getSnapshot().mode).toBe('off')
  })
  it('keeps a failed intent retry bounded and reports without resetting selection',async()=>{
    let attempts=0,reports=0;const c=new SearchModeController(async()=>{attempts++;throw Error('offline')},()=>{reports++})
    c.toggle('web');await expect(c.submission('制作需求')).rejects.toThrow('offline');await settle()
    expect(attempts).toBe(2);expect(reports).toBe(1);expect(c.state.getSnapshot().mode).toBe('web')
  })
  it('records an authenticated intent rather than invoking a search endpoint',async()=>{
    let path='',body='',interaction=''
    const request=(async(url:RequestInfo|URL,options?:RequestInit)=>{path=String(url);body=String(options?.body);interaction=(options?.headers as Record<string,string>)['X-Wuyu-Interaction'];return new Response('{}',{status:200})}) as typeof fetch
    await armResearchOnServer('web',request)
    expect(path).toBe('/agent-api/api/projects/research-intent');expect(JSON.parse(body)).toEqual({mode:'web'});expect(interaction).toBe('research-confirmation')
  })
})
