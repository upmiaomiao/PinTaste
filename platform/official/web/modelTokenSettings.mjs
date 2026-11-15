import {t,getUiLanguage} from './uiLanguage.mjs';
export const MAX_TOKEN_INTEGER = Number.MAX_SAFE_INTEGER;
export const INPUT_HEADROOM = 1025;

export function tokenSettingsError(cfg) {
  const context = Number(cfg.context_window), output = Number(cfg.max_output_tokens);
  if (!Number.isSafeInteger(context) || context < 8192) return t("上下文容量须为不少于 8192 的整数。");
  if (!Number.isSafeInteger(output) || output < 512) return t("最大输出须为不少于 512 的整数。");
  if (output > context - INPUT_HEADROOM) return t("最大输出须小于上下文容量，并至少为输入保留 1025 tokens。");
  return '';
}

export function syncTokenBudgets(cfg) {
  const error = tokenSettingsError(cfg);
  if (error) throw new Error(error);
  const contexts = [Number(cfg.context_window), ...(cfg.additional_models || [])
    .filter(p => p.enabled).map(p => Number(p.context_window))];
  const capacity = Math.max(...contexts), parallel = Number(cfg.max_parallel);
  if (!contexts.every(v => Number.isSafeInteger(v) && v >= 8192) || !Number.isSafeInteger(parallel) || parallel < 1)
    throw new Error(t("请先填写有效的模型上下文与平台同时生成数。"));
  const task = capacity * 4, inflight = capacity * parallel;
  if (!Number.isSafeInteger(task) || !Number.isSafeInteger(inflight)) throw new Error(t("同步后的预算超出可保存的整数范围，请降低上下文容量或并发数。"));
  return {...cfg,
    context_window: Number(cfg.context_window), max_output_tokens: Number(cfg.max_output_tokens),
    busy_output_tokens: Number(cfg.max_output_tokens),
    job_token_budget: Math.max(Number(cfg.job_token_budget) || 0, task),
    inflight_token_budget: Math.max(Number(cfg.inflight_token_budget) || 0, inflight),
    job_timeout: Math.max(Number(cfg.job_timeout) || 0, 21600),
  };
}

export function longAnswerPreset(cfg) {
  return syncTokenBudgets({...cfg, context_window: 260000, max_output_tokens: 160000});
}
