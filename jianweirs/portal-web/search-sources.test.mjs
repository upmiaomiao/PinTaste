import assert from 'node:assert/strict';
import test from 'node:test';
import {renderSearchSources,sourceCitations} from './search-sources.mjs';

test('source footer shows distinct providers, publication metadata and saved article content',()=>{
 const policy={channels:{webpage:{status:'completed',source_count:8},wechat:{status:'partial',source_count:1}},sources:[
  ...Array.from({length:8},(_,i)=>({id:`P00${i+1}`,source_type:'webpage',title:'网页',url:`https://example.com/${i}`})),
  {id:'P009',source_type:'wechat',title:'正文标题',url:'http://152.136.236.199:8077/articles/test',account:'测试公众号',published_at:'2025-04-07T00:00:00Z',snapshot_url:'/portal/api/tasks/task1/sources/P009',content_status:'partial',metadata_conflicts:['date mismatch']}]};
 const html=renderSearchSources(policy,['P009'],'task1');
 assert.ok(html.includes('微信公众号')&&html.includes('测试公众号')&&html.includes('2025/04/07'));
 assert.ok(html.indexOf('正文标题')<html.indexOf('查看其余'));
 assert.ok(html.indexOf('正文标题')<html.indexOf('https://example.com/1'));
 assert.ok(html.includes('已读正文片段')&&html.includes('显示正文日期')&&html.includes('用于回答'));
 assert.ok(html.includes('/portal/api/tasks/task1/sources/P009'));
 assert.equal(sourceCitations('依据[P009]，未知[P999]',policy,'task1'),'依据[P009](#source-task1-P009)，未知[P999]');
});

test('untrusted source text is escaped and executable URLs are excluded',()=>{
 const html=renderSearchSources({sources:[{id:'P001',title:'<img src=x onerror=alert(1)>',account:'<script>bad</script>',source_type:'wechat',url:'https://example.com',snapshot_url:'javascript:alert(1)'},{id:'P002',title:'bad',url:'javascript:alert(1)'}]},[],'t');
 assert.ok(!html.includes('<img')&&!html.includes('<script>')&&!html.includes('javascript:'));
 assert.ok(html.includes('&lt;img')&&html.includes('&lt;script&gt;'));
});
