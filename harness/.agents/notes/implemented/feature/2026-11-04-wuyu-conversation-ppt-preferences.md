# Conversation-owned PPT preferences

Status: implemented

The work composer persists count, output and content-language preferences independently of project generation. An authenticated exact Host route resolves an owned root Session before reading or writing a private per-Session JSON file under DSH_HOME. Revision checks prevent stale windows from overwriting newer settings. Atomic replacement and request identities protect writes and uncertain retries. No schema migration or project operation is needed.

The browser holds only editable drafts and acknowledgement state. Sending blocks until preferences are loaded and valid changes acknowledged, before research can be armed. Effective settings enter ordinary logged user text. Explicit unquoted Chinese preference changes update the same state; a one-request override does not replace conversation defaults. The scoped controller ignores late responses after disposal. Model prompts distinguish preferences from template, draft, image and paid-operation approval.

Using browser sessionStorage would lose cross-device restoration. Extending the core Session format would couple this small rollout to framework readers. Private sidecar files preserve existing logs; they are deployment-local and must accompany account-home backups. Deleted Sessions may leave a small orphaned preference file; automatic cleanup and live multi-window push are deferred. Template preferences remain in the existing preview/confirmation interface. User phrasing outside the explicit parser remains ordinary conversation and is not promised to synchronize controls.

Verification covers real HTTP authentication, Session ownership, storage restart/reload, independent conversations, revision races, duplicate requests, symlink refusal, invalid counts, client failures and request summaries. The actual assembled test workbench is checked with model-bound submission intercepted. Full-framework checks retain the known deployment dependency limitations; paid generation is outside this rollout.
