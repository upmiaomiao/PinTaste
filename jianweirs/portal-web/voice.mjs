import { escapeHTML as esc } from "./lib.mjs";
import { portalAsset } from "./ui-config.mjs";
import { PcmUploadQueue } from "./voice-recorder.mjs";

const MIC =
  '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10v2a7 7 0 0 0 14 0v-2M12 19v3m-4 0h8"/></svg>';

export function voiceConfig(value) {
  const config = value?.voice;
  if (!config || typeof config.ready !== "boolean")
    throw Error("语音服务暂时无法读取，请稍后重试。");
  const seconds = Number(config.max_seconds);
  if (
    config.ready &&
    (!Number.isFinite(seconds) || seconds < 1 || seconds > 120)
  )
    throw Error("语音服务配置无效，请联系管理员。");
  return {
    ready: config.ready,
    max_seconds: seconds || 120,
    recognition_timeout: Math.max(
      5,
      Math.min(120, Number(config.recognition_timeout) || 30),
    ),
  };
}

export function appendTranscript(current, transcript, maxLength = 12000) {
  const previous = typeof current === "string" ? current : "";
  const speech =
    typeof transcript === "string"
      ? transcript
          .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/g, "")
          .trim()
      : "";
  const separator = previous.trim() && speech ? "\n" : "";
  const full = previous + separator + speech;
  return { text: full.slice(0, maxLength), truncated: full.length > maxLength };
}

export function voiceError(error) {
  if (error?.name === "NotAllowedError" || error?.name === "SecurityError")
    return "请允许浏览器使用麦克风后重试。";
  if (error?.name === "NotFoundError" || error?.name === "DevicesNotFoundError")
    return "未找到麦克风，请连接设备后重试。";
  if (error?.name === "NotReadableError" || error?.name === "TrackStartError")
    return "麦克风正被其他程序占用，请关闭占用后重试。";
  return typeof error?.message === "string" && error.message
    ? error.message.slice(0, 500)
    : "无法开始录音，请检查麦克风和网络后重试。";
}

/**
 * Render with button()/panel(), delegate voice-* actions, and call context(key)
 * on task/user navigation. onText only inserts text: this controller never sends
 * a question. The central platform owns the authenticated ASR session and keys.
 */
export function createVoiceController(state, options = {}) {
  const env = options.env || globalThis;
  const notice = options.notice || (() => {});
  const changed = options.onChange || (() => {});
  const onText = options.onText || (() => {});
  const isDisabled =
    options.isDisabled ||
    (() => !!state.uploading || !!state.busy || !!state.dialoguePending);
  const getFetch = () => options.fetch || env.fetch?.bind(env);
  let session = null,
    phase = "idle",
    partial = "",
    seconds = 0,
    contextKey = null;
  const alive = (s) => session === s && !s.closed;
  const notify = () =>
    changed({ phase, partial, seconds, active: phase !== "idle" });

  function releaseCapture(s) {
    s.stream?.getTracks().forEach((track) => track.stop());
    s.node?.disconnect();
    s.source?.disconnect();
    if (s.context && s.context.state !== "closed")
      Promise.resolve(s.context.close()).catch(() => {});
  }
  function release(s) {
    env.clearInterval(s.timer);
    for (const name of [
      "prepareDeadline",
      "connectDeadline",
      "captureDeadline",
      "recognitionDeadline",
      "flushTimer",
      "cueTimer",
    ])
      env.clearTimeout(s[name]);
    s.abort?.abort();
    releaseCapture(s);
    s.upload.clear();
    if (s.socket?.readyState < 2) s.socket.close();
  }
  function close() {
    const s = session;
    if (s) {
      s.closed = true;
      session = null;
      release(s);
    }
    phase = "idle";
    partial = "";
    seconds = 0;
    notify();
  }
  function fail(s, text) {
    if (!alive(s)) return;
    close();
    notice(text, true);
  }
  function drain(s) {
    if (!alive(s)) return;
    try {
      if (s.upload.flush(s.socket) && !s.recognitionDeadline)
        s.recognitionDeadline = env.setTimeout(
          () => fail(s, "语音识别超时，请重试。"),
          (s.config.recognition_timeout + 5) * 1000,
        );
    } catch (error) {
      fail(s, voiceError(error));
    }
  }
  function stop() {
    const s = session;
    if (!s || !alive(s) || s.ending || !s.node) return;
    s.ending = true;
    phase = "finishing";
    notify();
    env.clearInterval(s.timer);
    env.clearTimeout(s.cueTimer);
    env.clearTimeout(s.captureDeadline);
    s.flushTimer = env.setTimeout(
      () => fail(s, "录音收尾超时，请重新录制。"),
      3000,
    );
    s.node.port.postMessage({ type: "flush" });
    s.stream?.getTracks().forEach((track) => track.stop());
  }
  async function start() {
    if (session || isDisabled()) return;
    if (!state.user || state.auth?.mode !== "wuyu") {
      notice("请使用无隅 AI 账号登录后使用语音输入。", true);
      return;
    }
    const s = { closed: false, upload: new PcmUploadQueue(120) };
    session = s;
    phase = "preparing";
    partial = "";
    seconds = 0;
    notify();
    try {
      if (
        !env.navigator?.mediaDevices?.getUserMedia ||
        !env.AudioWorkletNode ||
        !(env.AudioContext || env.webkitAudioContext)
      )
        throw Error(
          "录音需要 HTTPS 或本机 localhost，以及支持麦克风的浏览器。",
        );
      s.prepareDeadline = env.setTimeout(
        () => fail(s, "录音准备超时，请检查麦克风权限后重试。"),
        30000,
      );
      s.abort = new env.AbortController();
      const configDeadline = env.setTimeout(() => s.abort.abort(), 10000);
      let response;
      try {
        response = await getFetch()("/api/config", {
          credentials: "same-origin",
          signal: s.abort.signal,
        });
      } finally {
        env.clearTimeout(configDeadline);
      }
      if (!alive(s)) return;
      if (response.status === 401)
        throw Error("登录状态已过期，请重新登录后使用语音。");
      if (!response.ok) throw Error("语音服务暂时无法连接，请稍后重试。");
      s.config = voiceConfig(await response.json());
      if (!s.config.ready) throw Error("管理员尚未启用平台语音输入。");
      s.upload = new PcmUploadQueue(s.config.max_seconds);
      s.stream = await env.navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
        },
      });
      if (!alive(s)) {
        release(s);
        return;
      }
      const AudioContext = env.AudioContext || env.webkitAudioContext;
      s.context = new AudioContext();
      await s.context.resume();
      if (!alive(s)) {
        release(s);
        return;
      }
      await s.context.audioWorklet.addModule(
        portalAsset("recorder-worklet.js"),
      );
      if (!alive(s)) {
        release(s);
        return;
      }
      s.source = s.context.createMediaStreamSource(s.stream);
      s.node = new env.AudioWorkletNode(s.context, "wuyu-pcm-recorder");
      s.node.onprocessorerror = () => fail(s, "麦克风录音中断，请重新录制。");
      s.node.port.onmessage = ({ data }) => {
        if (!alive(s)) return;
        if (data?.type === "pcm") {
          try {
            s.upload.push(data.buffer);
          } catch (error) {
            fail(s, voiceError(error));
            return;
          }
          if (s.started === undefined) {
            env.clearTimeout(s.captureDeadline);
            env.clearTimeout(s.prepareDeadline);
            s.started = env.performance.now();
            // Match the platform: do not cue the user before local PCM exists.
            s.cueTimer = env.setTimeout(() => {
              if (alive(s) && !s.ending) {
                phase = "recording";
                notify();
              }
            }, 600);
            s.timer = env.setInterval(() => {
              const elapsed = (env.performance.now() - s.started) / 1000;
              const nextSeconds = Math.floor(elapsed);
              if (seconds !== nextSeconds) {
                seconds = nextSeconds;
                notify();
              }
              if (elapsed >= s.upload.maxBytes / 32000 - 0.25) stop();
            }, 100);
          }
          drain(s);
          if (alive(s) && s.upload.atLimit) stop();
        } else if (data?.type === "flushed") {
          env.clearTimeout(s.flushTimer);
          releaseCapture(s);
          s.upload.finish();
          drain(s);
        }
      };
      s.captureDeadline = env.setTimeout(
        () => fail(s, "麦克风未返回录音，请检查设备后重试。"),
        5000,
      );
      s.source.connect(s.node);
      s.node.connect(s.context.destination);
      const url = new URL("/api/voice/stream", env.location.origin);
      url.protocol = env.location.protocol === "https:" ? "wss:" : "ws:";
      s.socket = new env.WebSocket(url.href);
      s.connectDeadline = env.setTimeout(
        () => fail(s, "语音服务连接超时，请重试。"),
        20000,
      );
      s.socket.onmessage = (event) => {
        if (!alive(s)) return;
        let data;
        try {
          if (typeof event.data !== "string" || event.data.length > 65536)
            throw Error();
          data = JSON.parse(event.data);
        } catch {
          fail(s, "语音服务返回格式异常，请重试。");
          return;
        }
        if (data.type === "ready") {
          env.clearTimeout(s.connectDeadline);
          try {
            s.upload.setReady(data.max_seconds);
          } catch (error) {
            fail(s, voiceError(error));
            return;
          }
          drain(s);
          if (alive(s) && s.upload.atLimit) stop();
        } else if (data.type === "partial") {
          partial =
            typeof data.text === "string" ? data.text.slice(0, 20000) : "";
          notify();
        } else if (data.type === "final") {
          if (!s.ending || !s.upload.ended) {
            fail(s, "语音结果提前结束，请重新录制。");
            return;
          }
          const value =
            typeof data.text === "string"
              ? data.text.trim().slice(0, 20000)
              : "";
          close();
          if (value) onText(value, { seconds: Number(data.seconds) || 0 });
          else notice("未识别到语音，请重新录制。", true);
        } else if (data.type === "error")
          fail(
            s,
            typeof data.detail === "string"
              ? data.detail.slice(0, 500)
              : "语音识别失败，请重试。",
          );
      };
      s.socket.onerror = () => fail(s, "无法连接语音服务，请检查网络后重试。");
      s.socket.onclose = (event) => {
        if (!alive(s)) return;
        fail(
          s,
          event.code === 4401
            ? "登录状态已过期，请重新登录。"
            : event.code === 4403
              ? "当前账号暂不能使用语音，请联系平台管理员。"
              : "语音连接已关闭，请重新录制。",
        );
      };
    } catch (error) {
      if (!alive(s)) {
        release(s);
        return;
      }
      fail(
        s,
        error?.name === "AbortError"
          ? "语音配置读取超时，请重试。"
          : voiceError(error),
      );
    }
  }
  const hide = () => {
    if (env.document?.hidden) close();
  };
  const pagehide = () => close();
  env.document?.addEventListener("visibilitychange", hide);
  env.addEventListener?.("pagehide", pagehide);
  return {
    isActive: () => phase !== "idle",
    context(key) {
      if (contextKey !== null && key !== contextKey && session) close();
      contextKey = key;
    },
    close,
    destroy() {
      close();
      env.document?.removeEventListener("visibilitychange", hide);
      env.removeEventListener?.("pagehide", pagehide);
    },
    button() {
      return `<button type="button" class="composer-voice icon-button ${phase === "recording" ? "recording" : ""}" data-action="voice-start" aria-label="语音输入" title="语音输入，录音完成后核对文字" ${isDisabled() || phase !== "idle" ? "disabled" : ""}>${MIC}</button>`;
    },
    panel() {
      if (phase === "idle") return "";
      const title =
        phase === "preparing"
          ? "正在准备录音…"
          : phase === "recording"
            ? `可以开始说话 · ${seconds}s`
            : "正在转写…";
      const hint =
        phase === "preparing"
          ? "请等到“可以开始说话”后开口。"
          : phase === "recording"
            ? "完成录音后，我会把文字填入输入框，核对后再发送。"
            : "识别结果将填入输入框，请稍候。";
      return `<section class="voice-panel" role="region" aria-label="语音录入" data-phase="${phase}"><div class="voice-panel-row"><span class="${phase === "recording" ? "voice-live-dot" : "spin"}" aria-hidden="true"></span><strong role="status">${esc(title)}</strong>${phase === "recording" ? '<button type="button" class="secondary" data-action="voice-stop">完成录音</button>' : ""}<button type="button" class="icon-button" data-action="voice-cancel" aria-label="取消录音">×</button></div><p aria-live="polite">${esc(partial || hint)}</p></section>`;
    },
    async action(action) {
      if (action === "voice-start") {
        await start();
        return true;
      }
      if (action === "voice-stop") {
        stop();
        return true;
      }
      if (action === "voice-cancel") {
        close();
        return true;
      }
      return false;
    },
  };
}
