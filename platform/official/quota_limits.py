"""Operator-owned per-account quota exemption; never a client preference."""
from . import store as db

def unlimited(user_id, *, conn=None):
    row = (db.query('SELECT value FROM settings WHERE key=?', ('quota_unlimited_' + user_id,), one=True) if conn is None else
           conn.execute('SELECT value FROM settings WHERE key=?', ('quota_unlimited_' + user_id,)).fetchone())
    return bool(row and row['value'] == 'true')

def job_reservation(cfg):
    """Keep the operator cap and four-window allowance for this selected model."""
    limit, window = cfg['job_token_budget'], cfg['context_window']
    if type(limit) is not int or type(window) is not int or limit <= 0 or window <= 0:
        raise ValueError('Invalid model reservation limits')
    return min(limit, 4 * window)


def daily_exceeded(user_id, cfg, questions, tokens, *, reservation=None, conn=None):
    required = cfg['job_token_budget'] if reservation is None else reservation
    if type(required) is not int or required <= 0:
        raise ValueError('Invalid task reservation')
    exempt = unlimited(user_id) if conn is None else unlimited(user_id, conn=conn)
    return not exempt and (questions >= cfg['daily_questions'] or
        tokens + required > cfg['daily_tokens'])


def daily_exceeded_message(cfg, questions, tokens, held, *, reservation):
    """Explain the existing admission decision; do not change its thresholds."""
    if questions >= cfg['daily_questions']:
        return '今日提问次数已达上限，请次日再试。'
    if reservation > cfg['daily_tokens']:
        return '本次任务的预留额度超过账号日额度，请联系管理员核对任务预算或选择其他模型。'
    if held > 0 and tokens - held + reservation <= cfg['daily_tokens']:
        return '已有任务正在预留可用额度，请等待这些任务结束后再试；这不代表今日额度已经用完。'
    return '今日可用额度不足以预留本次任务，请次日再试或选择预留需求更低的模型。'
