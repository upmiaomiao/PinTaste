/* Same PCM16 protocol as the shared Wuyu platform, without provider credentials. */
const BYTES_PER_SECOND = 32000;
const MAX_PENDING_BYTES = 10 * BYTES_PER_SECOND;

export class PcmUploadQueue {
  constructor(maxSeconds = 120) {
    this.maxBytes =
      Math.floor(Math.max(1, Math.min(120, Number(maxSeconds) || 120))) *
      BYTES_PER_SECOND;
    this.frames = [];
    this.capturedBytes = 0;
    this.sentBytes = 0;
    this.pendingBytes = 0;
    this.ready = false;
    this.finished = false;
    this.ended = false;
  }
  get atLimit() {
    return this.capturedBytes >= this.maxBytes;
  }
  push(buffer) {
    if (this.finished || this.ended) return;
    if (
      !(buffer instanceof ArrayBuffer) ||
      !buffer.byteLength ||
      buffer.byteLength % 2 ||
      buffer.byteLength > 1280
    )
      throw Error("录音数据异常，请重新录制。");
    const remaining = this.maxBytes - this.capturedBytes;
    if (remaining <= 0) return;
    const chunk =
      buffer.byteLength > remaining ? buffer.slice(0, remaining) : buffer;
    if (this.pendingBytes + chunk.byteLength > MAX_PENDING_BYTES)
      throw Error("语音连接等待过久，请检查网络后重新录制。");
    this.frames.push(chunk);
    this.capturedBytes += chunk.byteLength;
    this.pendingBytes += chunk.byteLength;
  }
  setReady(maxSeconds) {
    if (this.ready) return;
    const seconds = Number(maxSeconds);
    if (!Number.isFinite(seconds) || seconds < 1 || seconds > 120)
      throw Error("语音服务返回的录音时限无效。");
    this.maxBytes = Math.min(
      this.maxBytes,
      Math.floor(seconds) * BYTES_PER_SECOND,
    );
    if (this.capturedBytes > this.maxBytes) {
      let remaining = this.maxBytes - this.sentBytes;
      this.frames = this.frames.flatMap((frame) => {
        if (remaining <= 0) return [];
        const part =
          frame.byteLength > remaining ? frame.slice(0, remaining) : frame;
        remaining -= part.byteLength;
        return [part];
      });
      this.capturedBytes = this.maxBytes;
      this.pendingBytes = this.maxBytes - this.sentBytes;
    }
    this.ready = true;
  }
  finish() {
    this.finished = true;
  }
  flush(socket) {
    if (!this.ready || this.ended || socket?.readyState !== 1) return false;
    while (this.frames.length) {
      if (socket.bufferedAmount > 256000)
        throw Error("上传速度不足，请缩短录音后重试。");
      const frame = this.frames[0];
      socket.send(frame);
      this.frames.shift();
      this.pendingBytes -= frame.byteLength;
      this.sentBytes += frame.byteLength;
    }
    if (this.finished) {
      if (!this.sentBytes) throw Error("尚未录到声音，请重新录制。");
      socket.send(JSON.stringify({ type: "end" }));
      this.ended = true;
      return true;
    }
    return false;
  }
  clear() {
    this.frames = [];
    this.pendingBytes = 0;
    this.finished = true;
    this.ended = true;
  }
}
