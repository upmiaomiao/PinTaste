/** Same PCM16/16kHz queue protocol as the question platform, with locale-neutral errors. */
export class PcmUploadQueue {
  maxBytes:number
  private frames:ArrayBuffer[]=[]
  private captured=0
  private sent=0
  private pending=0
  private ready=false
  private finished=false
  private ended=false
  constructor(seconds:number){this.maxBytes=Math.floor(Math.max(1,Math.min(120,seconds||120)))*32000}
  get atLimit():boolean{return this.captured>=this.maxBytes}
  push(buffer:ArrayBuffer):void{
    if(this.finished||this.ended)return
    if(!(buffer instanceof ArrayBuffer)||!buffer.byteLength||buffer.byteLength%2||buffer.byteLength>1280)throw new Error('VOICE_PCM_INVALID')
    const left=this.maxBytes-this.captured;if(left<=0)return
    const chunk=buffer.byteLength>left?buffer.slice(0,left):buffer
    if(this.pending+chunk.byteLength>320000)throw new Error('VOICE_NETWORK_SLOW')
    this.frames.push(chunk);this.captured+=chunk.byteLength;this.pending+=chunk.byteLength
  }
  setReady(seconds:number):void{
    if(this.ready)return
    this.maxBytes=Math.min(this.maxBytes,Math.floor(Math.max(1,seconds||120))*32000)
    if(this.captured>this.maxBytes){let left=this.maxBytes-this.sent
      this.frames=this.frames.flatMap(frame=>{if(left<=0)return [];const chunk=frame.byteLength>left?frame.slice(0,left):frame;left-=chunk.byteLength;return [chunk]})
      this.captured=this.maxBytes;this.pending=this.maxBytes-this.sent
    }
    this.ready=true
  }
  finish():void{this.finished=true}
  flush(socket:Pick<WebSocket,'readyState'|'bufferedAmount'|'send'>|null):boolean{
    if(!this.ready||this.ended||socket?.readyState!==1)return false
    while(this.frames.length){if(socket.bufferedAmount>256000)throw new Error('VOICE_NETWORK_SLOW')
      const frame=this.frames[0]!;socket.send(frame);this.frames.shift();this.pending-=frame.byteLength;this.sent+=frame.byteLength}
    if(this.finished){socket.send(JSON.stringify({type:'end'}));this.ended=true;return true}
    return false
  }
  clear():void{this.frames=[];this.pending=0;this.finished=true;this.ended=true}
}
