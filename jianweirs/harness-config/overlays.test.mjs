import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import vm from 'node:vm'
import test from 'node:test'

const require = createRequire(new URL('../deepseek-harness/vendor/include/package.json',import.meta.url))
const yaml = require('js-yaml')
const expr = new yaml.Type('tag:yaml.org,2002:js',{kind:'scalar',construct:value=>({__jsExpr:value})})
const schema = yaml.JSON_SCHEMA.extend(expr)
const load = name => yaml.load(readFileSync(new URL(name,import.meta.url),'utf8'),{schema})
const evaluate = (value,env) => value?.__jsExpr
  ? vm.runInNewContext(value.__jsExpr,{process:{env,cwd:()=>'/fixture'}})
  : value

test('compatible business main route remains text only even when the endpoint accepts images',()=>{
  const overlay=load('wuyu-main-compatible.cordis.yml')
  const provider=overlay.find(row=>row.id==='llm-pi-ai').config.providers['wuyu-main']
  assert.equal(provider.api,'openai-completions')
  assert.equal(provider.apiKeyEnv,'WUYU_MAIN_API_KEY')
  const model=provider.models[0]
  assert.deepEqual(Array.from(evaluate(model.input,{WUYU_MAIN_BASE_URL:'https://api.deepseek.com/v1',WUYU_MAIN_MODEL:'deepseek-flash'})),['text'])
  assert.deepEqual(Array.from(evaluate(model.input,{WUYU_MAIN_BASE_URL:'https://unknown.example/v1'})),['text'])
  assert.deepEqual(Array.from(evaluate(model.input,{WUYU_MAIN_BASE_URL:'https://api.deepseek.com/v1',WUYU_MAIN_INPUT:'text'})),['text'])
  assert.deepEqual(Array.from(evaluate(model.input,{WUYU_MAIN_BASE_URL:'https://www.dmxapi.cn/v1',WUYU_MAIN_INPUT:'text,image'})),['text'])
  assert.equal(evaluate(model.id,{}),'deepseek-flash')
})

test('business presets keep expert images in viewer artifacts rather than main read_image calls',()=>{
  const common=load('wuyu.cordis.yml')
  assert.equal(common.find(row=>row.id==='tool-fs').config.readImage,false)
  const web=load('wuyu-web.cordis.yml')
  const preset=web.find(row=>row.insert)?.insert.find(row=>row.id==='preset-wuyu')
  assert.equal(preset.config.plugins.find(row=>row.id==='tool-fs').config.readImage,false)
})

test('official Flash overlay exposes the official output and context capacity without forcing thinking off',()=>{
  const provider=load('wuyu-main-compatible.cordis.yml').find(row=>row.id==='llm-pi-ai').config.providers['wuyu-main']
  const model=provider.models[0],official={WUYU_MAIN_BASE_URL:'https://api.deepseek.com/v1',WUYU_MAIN_MODEL:'deepseek-flash'}
  assert.equal(evaluate(model.maxTokens,official),393216)
  assert.equal(evaluate(model.contextWindow,official),1048576)
  assert.equal(evaluate(provider.timeoutMs,official),3600000)
  assert.equal(evaluate(model.maxTokens,{WUYU_MAIN_BASE_URL:'https://custom.example/v1'}),8192)
  assert.equal(evaluate(model.contextWindow,{WUYU_MAIN_BASE_URL:'https://custom.example/v1'}),undefined)
  assert.equal(evaluate(provider.timeoutMs,{WUYU_MAIN_BASE_URL:'https://custom.example/v1'}),120000)
  assert.equal(evaluate(model.maxTokens,{...official,WUYU_MAIN_MAX_TOKENS:'65536'}),65536)
  assert.equal(evaluate(provider.timeoutMs,{...official,WUYU_MAIN_TIMEOUT:'1200'}),1200000)
  assert.equal(provider.reasoning,undefined)
  assert.equal(provider.thinkingBudgets,undefined)
})

test('all overlays parse with the upstream YAML expression tag and keep flexible evidence guidance',()=>{
  for(const file of ['wuyu.cordis.yml','wuyu-web.cordis.yml','wuyu-planner.cordis.yml']){
    assert.ok(Array.isArray(load(file)))
    const content=readFileSync(new URL(file,import.meta.url),'utf8')
    assert.match(content,/Gemini/)
    assert.match(content,/TCI/)
    assert.match(content,/补证/)
    assert.doesNotMatch(content,/一张合格影像发现疑点后须追加|只有多个合格相关来源均未发现才支持低|多个合格相关来源均未发现才支持低/)
  }
})
