# Disconnected workspace retirement

`idle_seconds` enables retirement of disconnected account Host/MCP processes. It defaults to zero (disabled); staging uses 900 seconds and a 30-second `idle_check_seconds` sweep. `idle_database_path` must identify the local backend SQLite file. The shared legacy administrator Host is excluded.

An open WebSocket, an HTTP request (including streaming uploads/downloads), a recent authenticated workspace request, native running sessions, or any unfinished backend workflow/legacy job prevents retirement. Background identity-history reads acquire a request lease but do not extend the idle deadline. Unknown native state, inaccessible/malformed/locked databases and failed history reads block retirement. The initial policy keeps connected pages awake; it does not close an idle page's socket or depend on browser activity detection.

The Router retains a bounded, private account-owned session summary before graceful process shutdown. Disconnected history polling reads that summary without launching a Host. New accounts with no local home have empty native history. On an actual workspace visit, the normal authenticated launch reuses the same account home, workspace, keys and native session records. The summary is invalidated before waking to avoid returning stale data after a subsequent crash. Concurrent wake requests share one launch.

Only process resources and the in-memory slot are released. No session, project, material, generated image, PPT, index, database row or cloud object is removed. Both processes must exit and both allocated ports must be free before the slot is reusable. A failed graceful shutdown retains the slot and blocks access until recovery; automatic idle shutdown never sends SIGKILL.

The existing authenticated `/__internal/activity` endpoint reports idle policy, retirement/wake counters and active request counts. No RPC is newly permitted. The backend guard reads status columns only with SQLite read-only/query-only mode and is conservative across all accounts. This first version may retain an otherwise idle workspace while another backend job is active.

Disable by setting `idle_seconds` to zero, then refresh the Router only when work is idle. Persistent account files remain usable by the previous Router version. Remove no databases or account directories during rollback.

Legacy backend tasks with PARTIAL status are terminal; durable workflow jobs do not use that status and remain protected if it appears. This preserves compatibility with existing completed production task records.
