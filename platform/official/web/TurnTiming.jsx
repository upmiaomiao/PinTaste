import React, { useEffect, useMemo, useState } from 'react';
import { t } from './i18n.mjs';
import { elapsedTiming } from './executionTiming.mjs';
import './execution-timing.css';
const clock = () => typeof performance !== 'undefined' ? performance.now() : Date.now();
const seconds = value => Number.isFinite(value) && value >= 0 ? value.toFixed(1) : null;
export default function TurnTiming({ turn }) {
  const active = ['queued', 'running'].includes(turn.status);
  const anchor = useMemo(() => clock(), [turn.id, turn.executionTiming?.sampled_at]);
  const [now, setNow] = useState(clock);
  useEffect(() => {
    if (!active) return undefined;
    const interval = setInterval(() => setNow(clock()), 500);
    return () => clearInterval(interval);
  }, [active]);
  const timing = elapsedTiming(turn, Math.max(0, (now - anchor) / 1000));
  const agent = seconds(turn.meta?.seconds), first = seconds(turn.meta?.first_token_seconds);
  const parts = [
    ['queue_seconds', '排队 {v0} 秒'], ['execution_seconds', '执行 {v0} 秒'], ['total_seconds', '总等待 {v0} 秒'],
  ];
  if (!timing && agent === null && first === null) return null;
  return <div className="execution-timing" aria-label={t('任务耗时')}>
    {parts.map(([key, label]) => timing?.[key] != null && <span key={key}>{t(label, { v0: seconds(timing[key]) })}</span>)}
    {agent !== null && <span title={t('从模型与工具流程开始计时，不含排队及部分准备时间。')}>{t('模型与工具处理 {v0} 秒', { v0: agent })}</span>}
    {first !== null && <span title={t('可能是思考内容，不代表最终答案首字；从模型与工具流程开始计时。')}>{t('首段输出 {v0} 秒', { v0: first })}</span>}
  </div>;
}
