import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";
import { PcmUploadQueue } from "./voice-recorder.mjs";
import {
  appendTranscript,
  createVoiceController,
  voiceConfig,
  voiceError,
} from "./voice.mjs";

test("recording keeps opening PCM until authenticated ready, then ends exactly once", () => {
  const queue = new PcmUploadQueue(2),
    sent = [];
  const socket = {
    readyState: 1,
    bufferedAmount: 0,
    send: (frame) => sent.push(frame),
  };
  queue.push(new ArrayBuffer(1280));
  queue.push(new ArrayBuffer(640));
  queue.finish();
  assert.equal(queue.flush(socket), false);
  assert.equal(sent.length, 0);
  queue.setReady(2);
  assert.equal(queue.flush(socket), true);
  assert.deepEqual(
    sent.map((frame) => (typeof frame === "string" ? frame : frame.byteLength)),
    [1280, 640, '{"type":"end"}'],
  );
  assert.equal(queue.flush(socket), false);
  assert.equal(sent.length, 3);
});

test("recording validates frame format and bounds handshake memory and audio duration", () => {
  const queue = new PcmUploadQueue(1);
  for (const invalid of [
    new ArrayBuffer(1),
    new ArrayBuffer(1282),
    new ArrayBuffer(0),
    "data",
  ])
    assert.throws(() => queue.push(invalid), /录音数据异常/);
  for (let i = 0; i < 27; i++) queue.push(new ArrayBuffer(1280));
  assert.equal(queue.capturedBytes, 32000);
  assert.equal(queue.atLimit, true);
  const buffered = new PcmUploadQueue(120);
  for (let i = 0; i < 250; i++) buffered.push(new ArrayBuffer(1280));
  assert.throws(() => buffered.push(new ArrayBuffer(1280)), /等待过久/);
  buffered.setReady(1);
  assert.equal(buffered.pendingBytes, 32000);
  assert.throws(
    () => buffered.flush({ readyState: 1, bufferedAmount: 300000, send() {} }),
    /上传速度不足/,
  );
  buffered.clear();
  assert.equal(buffered.frames.length, 0);
});

test("voice config projects only public controls and drafts preserve existing user text", () => {
  assert.deepEqual(
    voiceConfig({
      voice: {
        ready: true,
        max_seconds: 120,
        recognition_timeout: 30,
        api_key: "never-copy",
      },
    }),
    { ready: true, max_seconds: 120, recognition_timeout: 30 },
  );
  assert.throws(
    () => voiceConfig({ voice: { ready: true, max_seconds: 900 } }),
    /配置无效/,
  );
  assert.throws(() => voiceConfig({}), /无法读取/);
  assert.deepEqual(appendTranscript("原来的问题", "请核查河边"), {
    text: "原来的问题\n请核查河边",
    truncated: false,
  });
  assert.deepEqual(appendTranscript("已有", "新内容", 4), {
    text: "已有\n新",
    truncated: true,
  });
  assert.equal(
    voiceError({ name: "NotAllowedError" }),
    "请允许浏览器使用麦克风后重试。",
  );
  assert.match(voiceError({ name: "NotFoundError" }), /未找到麦克风/);
});

function fixture({
  ready = true,
  mediaError,
  mediaPromise,
  authMode = "wuyu",
} = {}) {
  const sent = [],
    notices = [],
    texts = [],
    changes = [],
    sockets = [],
    nodes = [],
    listeners = new Map(),
    timers = new Map();
  let timerId = 0,
    now = 0,
    stopped = 0,
    closed = 0,
    mediaCalls = 0,
    fetchCalls = 0;
  const stream = {
    getTracks: () => [
      {
        stop() {
          stopped++;
        },
      },
    ],
  };
  class AudioContext {
    constructor() {
      this.state = "running";
      this.destination = {};
      this.audioWorklet = {
        addModule: async (url) => {
          assert.match(url, /recorder-worklet\.js$/);
        },
      };
    }
    resume() {
      return Promise.resolve();
    }
    createMediaStreamSource() {
      return { connect() {}, disconnect() {} };
    }
    close() {
      closed++;
      this.state = "closed";
      return Promise.resolve();
    }
  }
  class Node {
    constructor() {
      this.port = {
        postMessage: (message) => {
          if (message.type === "flush")
            this.port.onmessage?.({ data: { type: "flushed" } });
        },
      };
      nodes.push(this);
    }
    connect() {}
    disconnect() {}
  }
  class Socket {
    constructor(url) {
      assert.equal(url, "wss://ai.wuyullm.com/api/voice/stream");
      this.readyState = 0;
      this.bufferedAmount = 0;
      sockets.push(this);
    }
    send(packet) {
      sent.push(packet);
    }
    close() {
      this.readyState = 3;
    }
    receive(packet) {
      if (packet.type === "ready") this.readyState = 1;
      this.onmessage?.({ data: JSON.stringify(packet) });
    }
  }
  const env = {
    location: { origin: "https://ai.wuyullm.com", protocol: "https:" },
    navigator: {
      mediaDevices: {
        getUserMedia: async () => {
          mediaCalls++;
          if (mediaError) throw mediaError;
          return mediaPromise ? await mediaPromise : stream;
        },
      },
    },
    AudioContext,
    AudioWorkletNode: Node,
    WebSocket: Socket,
    AbortController,
    fetch: async (url, options) => {
      fetchCalls++;
      assert.equal(url, "/api/config");
      assert.equal(options.credentials, "same-origin");
      return {
        status: 200,
        ok: true,
        json: async () => ({
          voice: { ready, max_seconds: 120, recognition_timeout: 30 },
        }),
      };
    },
    performance: { now: () => now },
    document: {
      hidden: false,
      addEventListener: (type, fn) => listeners.set(type, fn),
      removeEventListener: (type) => listeners.delete(type),
    },
    addEventListener: (type, fn) => listeners.set(type, fn),
    removeEventListener: (type) => listeners.delete(type),
    setTimeout: (fn, ms) => {
      const id = ++timerId;
      timers.set(id, { fn, at: now + ms, interval: 0 });
      return id;
    },
    clearTimeout: (id) => timers.delete(id),
    setInterval: (fn, ms) => {
      const id = ++timerId;
      timers.set(id, { fn, at: now + ms, interval: ms });
      return id;
    },
    clearInterval: (id) => timers.delete(id),
  };
  const controller = createVoiceController(
    { user: { id: "self" }, auth: { mode: authMode } },
    {
      env,
      onText: (value) => texts.push(value),
      notice: (message) => notices.push(message),
      onChange: (value) => changes.push(value),
    },
  );
  return {
    controller,
    env,
    stream,
    sent,
    notices,
    texts,
    changes,
    nodes,
    sockets,
    listeners,
    stats: () => ({
      stopped,
      closed,
      mediaCalls,
      fetchCalls,
      timers: timers.size,
    }),
    advance(ms) {
      const end = now + ms;
      for (let limit = 0; limit < 10000; limit++) {
        const due = [...timers]
          .filter(([, timer]) => timer.at <= end)
          .sort((a, b) => a[1].at - b[1].at)[0];
        if (!due) break;
        const [id, timer] = due;
        now = timer.at;
        if (timer.interval) timer.at += timer.interval;
        else timers.delete(id);
        timer.fn();
      }
      now = end;
    },
    pcm() {
      nodes[0].port.onmessage({
        data: { type: "pcm", buffer: new ArrayBuffer(1280) },
      });
    },
  };
}

test("shared voice fills text only after user finishes and central final arrives", async () => {
  const f = fixture();
  await f.controller.action("voice-start");
  f.pcm();
  assert.equal(f.sent.length, 0);
  assert.equal(f.texts.length, 0);
  f.sockets[0].receive({ type: "ready", max_seconds: 120 });
  f.advance(600);
  assert.match(f.controller.panel(), /可以开始说话/);
  f.sockets[0].receive({
    type: "partial",
    text: "<script>not markup</script>",
  });
  assert.ok(!f.controller.panel().includes("<script>"));
  assert.match(f.controller.panel(), /&lt;script&gt;/);
  assert.deepEqual(
    f.sent.map((x) => x.byteLength),
    [1280],
  );
  await f.controller.action("voice-stop");
  assert.match(f.controller.panel(), /正在转写/);
  assert.equal(f.sent.at(-1), '{"type":"end"}');
  assert.equal(f.texts.length, 0);
  f.sockets[0].receive({
    type: "final",
    text: "请核查这处疑似堆存",
    seconds: 0.04,
  });
  assert.deepEqual(f.texts, ["请核查这处疑似堆存"]);
  assert.equal(f.controller.isActive(), false);
  assert.ok(f.stats().stopped >= 1);
  assert.equal(f.stats().timers, 0);
  f.controller.destroy();
  assert.equal(f.listeners.size, 0);
});

test("cancel during microphone permission releases a late stream and never opens ASR", async () => {
  let resolveMedia;
  const mediaPromise = new Promise((resolve) => {
    resolveMedia = resolve;
  });
  const f = fixture({ mediaPromise });
  const starting = f.controller.action("voice-start");
  for (let i = 0; i < 5; i++) await Promise.resolve();
  assert.equal(f.stats().mediaCalls, 1);
  await f.controller.action("voice-cancel");
  resolveMedia(f.stream);
  await starting;
  assert.equal(f.sockets.length, 0);
  assert.ok(f.stats().stopped >= 1);
  assert.equal(f.controller.isActive(), false);
  assert.deepEqual(f.texts, []);
  assert.equal(f.stats().timers, 0);
});

test("local accounts and unavailable provider do not capture audio", async () => {
  const local = fixture({ authMode: "local" });
  await local.controller.action("voice-start");
  assert.equal(local.stats().fetchCalls, 0);
  assert.equal(local.stats().mediaCalls, 0);
  assert.match(local.notices[0], /登录/);
  const off = fixture({ ready: false });
  await off.controller.action("voice-start");
  assert.equal(off.stats().mediaCalls, 0);
  assert.match(off.notices[0], /尚未启用/);
  const denied = fixture({ mediaError: { name: "NotAllowedError" } });
  await denied.controller.action("voice-start");
  assert.match(denied.notices[0], /允许浏览器使用麦克风/);
  assert.equal(denied.controller.isActive(), false);
});

test("task navigation, hidden tab and capture failure release resources", async () => {
  const f = fixture();
  f.controller.context("user:task1");
  await f.controller.action("voice-start");
  f.pcm();
  f.controller.context("user:task2");
  assert.equal(f.controller.isActive(), false);
  assert.ok(f.stats().stopped >= 1);
  assert.equal(f.stats().timers, 0);
  const hidden = fixture();
  await hidden.controller.action("voice-start");
  hidden.env.document.hidden = true;
  hidden.listeners.get("visibilitychange")();
  assert.equal(hidden.controller.isActive(), false);
  const silent = fixture();
  await silent.controller.action("voice-start");
  silent.advance(5000);
  assert.equal(silent.controller.isActive(), false);
  assert.match(silent.notices[0], /未返回录音/);
});

test("premature final and socket access failure do not insert unsupported text", async () => {
  const early = fixture();
  await early.controller.action("voice-start");
  early.sockets[0].receive({ type: "final", text: "do not insert" });
  assert.deepEqual(early.texts, []);
  assert.match(early.notices[0], /提前结束/);
  const denied = fixture();
  await denied.controller.action("voice-start");
  denied.sockets[0].onclose({ code: 4403 });
  assert.match(denied.notices[0], /账号暂不能/);
  assert.equal(denied.stats().timers, 0);
});

test("audio worklet resamples 48 kHz into bounded 16 kHz PCM16 frames", async () => {
  const source = await readFile(
    new URL("./recorder-worklet.js", import.meta.url),
    "utf8",
  );
  let Recorder;
  const packets = [];
  class Base {
    constructor() {
      this.port = { postMessage: (packet) => packets.push(packet) };
    }
  }
  vm.runInNewContext(source, {
    AudioWorkletProcessor: Base,
    sampleRate: 48000,
    Int16Array,
    Math,
    registerProcessor: (name, cls) => {
      assert.equal(name, "wuyu-pcm-recorder");
      Recorder = cls;
    },
  });
  const recorder = new Recorder();
  recorder.process([[new Float32Array(4800).fill(0.5)]]);
  recorder.port.onmessage({ data: { type: "flush" } });
  const frames = packets.filter((x) => x.type === "pcm");
  assert.deepEqual(
    frames.map((x) => x.buffer.byteLength),
    [1280, 1280, 640],
  );
  assert.equal(
    frames.reduce((n, x) => n + x.buffer.byteLength, 0),
    3200,
  );
  assert.equal(new Int16Array(frames[0].buffer)[0], 16384);
  assert.equal(packets.at(-1).type, "flushed");
});
