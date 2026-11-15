import assert from 'node:assert/strict';
import test from 'node:test';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
import {PcmUploadQueue} from './web/voice-recorder.mjs';

const frame=(value,length=640)=>new Int16Array(length).fill(value).buffer;
const socket=()=>({readyState:1,bufferedAmount:0,messages:[],send(value){this.messages.push(value);}});

test('opening audio survives a delayed ready message, including stopping during connection',()=>{
 const queue=new PcmUploadQueue(120),ws=socket();
 for(let i=1;i<=26;i++)queue.push(frame(i));
 queue.finish();assert.equal(queue.flush(ws),false);assert.equal(ws.messages.length,0);
 queue.setReady(120);assert.equal(queue.flush(ws),true);
 assert.deepEqual(ws.messages.slice(0,-1).map(value=>new Int16Array(value)[0]),Array.from({length:26},(_,i)=>i+1));
 assert.equal(ws.messages.at(-1),'\u007b"type":"end"\u007d');
 queue.flush(ws);assert.equal(ws.messages.length,27);assert.equal(queue.pendingBytes,0);
});
test('live frames and the final partial frame precede end without reordering',()=>{
 const queue=new PcmUploadQueue(120),ws=socket();queue.push(frame(11));queue.setReady(120);queue.flush(ws);
 queue.push(frame(22));queue.flush(ws);queue.push(frame(33,87));queue.finish();queue.flush(ws);
 assert.deepEqual(ws.messages.slice(0,-1).map(value=>[value.byteLength,new Int16Array(value)[0]]),[[1280,11],[1280,22],[174,33]]);
 assert.equal(queue.sentBytes,2734);
});
test('server duration cap retains the beginning and never exceeds the allowed PCM size',()=>{
 const queue=new PcmUploadQueue(120),ws=socket();for(let i=0;i<75;i++)queue.push(frame(i+1));
 queue.setReady(2);assert.equal(queue.atLimit,true);queue.push(frame(99));queue.finish();queue.flush(ws);
 assert.equal(queue.sentBytes,64000);assert.equal(ws.messages.length,51);
 assert.equal(new Int16Array(ws.messages[0])[0],1);assert.equal(new Int16Array(ws.messages[49])[0],50);
});
test('temporary connection state retains frames rather than silently dropping them',()=>{
 const queue=new PcmUploadQueue(120),ws=socket();ws.readyState=0;queue.push(frame(7));queue.setReady(120);
 assert.equal(queue.flush(ws),false);assert.equal(queue.pendingBytes,1280);ws.readyState=1;queue.flush(ws);
 assert.equal(new Int16Array(ws.messages[0])[0],7);
});
test('startup audio is bounded to ten seconds and fails explicitly on a stalled connection',()=>{
 const queue=new PcmUploadQueue(120);for(let i=0;i<250;i++)queue.push(frame(i));
 assert.throws(()=>queue.push(frame(251)),/等待过久/);assert.equal(queue.pendingBytes,320000);assert.equal(queue.capturedBytes,320000);
});
test('upload backpressure and malformed PCM produce explicit errors',()=>{
 const queue=new PcmUploadQueue(120),ws=socket();queue.push(frame(1));queue.setReady(120);ws.bufferedAmount=256001;
 assert.throws(()=>queue.flush(ws),/上传速度不足/);assert.equal(ws.messages.length,0);
 assert.throws(()=>queue.push(new ArrayBuffer(3)),/录音数据异常/);assert.throws(()=>queue.push(new ArrayBuffer(1282)),/录音数据异常/);
});
test('cancel discards pending audio and prevents any later upload or end event',()=>{
 const queue=new PcmUploadQueue(120),ws=socket();queue.push(frame(1));queue.setReady(120);queue.clear();queue.push(frame(2));queue.flush(ws);
 assert.equal(ws.messages.length,0);assert.equal(queue.pendingBytes,0);
});
test('the actual worklet captures the first samples, resamples stereo and flushes the final tail',()=>{
 let Recorder;const output=[];
 const sandbox={sampleRate:48000,Int16Array,AudioWorkletProcessor:class{constructor(){this.port={postMessage:value=>output.push(value)};}},registerProcessor:(_,value)=>{Recorder=value;}};
 vm.runInNewContext(readFileSync(new URL('./web/recorder-worklet.js',import.meta.url),'utf8'),sandbox);
 const recorder=new Recorder();const left=new Float32Array(128).fill(.6),right=new Float32Array(128).fill(.2);
 for(let i=0;i<16;i++)recorder.process([[left,right]]);
 recorder.port.onmessage({data:{type:'flush'}});
 const pcm=output.filter(value=>value.type==='pcm').map(value=>new Int16Array(value.buffer));
 assert.deepEqual(pcm.map(value=>value.length),[640,42]);assert.equal(pcm[0][0],13107);assert.equal(pcm[1].at(-1),13107);
 assert.equal(output.at(-1).type,'flushed');const count=output.length;recorder.process([[left,right]]);assert.equal(output.length,count);
});
