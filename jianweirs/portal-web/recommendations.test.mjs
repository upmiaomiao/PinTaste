import test from 'node:test';
import assert from 'node:assert/strict';
import {normaliseRecommendations,renderMarkdown} from './lib.mjs';

test('structured actions and reasons normalize without empty suggestion bullets',()=>{
 const raw=[null,'',' ',{}, {action:'',reason:'位置尚未核实'},
  {action:'核实实际拍摄日期',reason:'图源组名不能作为成像日期'},
  {action:'现场检查物料用途'},'现场检查物料用途',{unknown:'不得显示的对象'},'<think>private reasoning</think>','-',0,false,{action:null}];
 const result=normaliseRecommendations(raw);
 assert.deepEqual(result,['核实实际拍摄日期；图源组名不能作为成像日期','现场检查物料用途']);
 const html=renderMarkdown(result.map(value=>'- '+value).join('\n'));
 assert.equal((html.match(/<li>/g)||[]).length,2);assert.doesNotMatch(html,/<li>\s*<\/li>|\[object Object\]|private reasoning|位置尚未核实/);
 assert.deepEqual(normaliseRecommendations([null,{},'']),[]);
});
