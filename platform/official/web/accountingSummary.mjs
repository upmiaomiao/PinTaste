/** Platform quota attribution only; never a statement of provider payment. */
export function accountingSummary(accounting, language='zh-CN') {
  if (!accounting || typeof accounting !== 'object') return null;
  const {tokens, estimated, basis} = accounting;
  if (!Number.isSafeInteger(tokens) || tokens < 0) {
    return estimated === true ? {message:'本次用量按预留额度估算'} : null;
  }
  const values = {v0:tokens.toLocaleString(language)};
  if (estimated === true && basis === 'stopped_observed_estimate') {
    return {message:'本次平台计入 {v0} tokens（按已发送输入和已收到输出估算）',values};
  }
  if (estimated === true && basis === 'stopped_round_budget_estimate') {
    return {message:'本次平台计入 {v0} tokens（按各轮请求预算估算）',values};
  }
  if (estimated === true) {
    return {message:'本次平台计入 {v0} tokens（按预留额度估算）',values};
  }
  if (estimated === false && basis === 'stopped_reported_usage') {
    return {message:'本次平台计入 {v0} tokens（模型报告用量）',values};
  }
  return {message:'本次平台计入 {v0} tokens',values};
}
