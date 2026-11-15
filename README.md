# Wuyu staging source snapshot

Frontend and backend code exported from the staging source workspace on 2026-11-15.

Source base commit: `fbf862f59a784861dea7d9125c27a134f6d4f1ee`.
The snapshot also includes the uncommitted frontend and backend changes present at export time. It is a source snapshot, not a statement that every change has been deployed or accepted.

## Components

| Directory | Purpose |
| --- | --- |
| `platform` | Q&A platform frontend, API and worker code |
| `backend` | PPT backend, workflow worker and tests |
| `harness` | Workbench frontend and supporting packages |
| `work-agent` | Workbench tool bridge, maintained runtime assets and UI tools |
| `router` | Routing and model gateway code |
| `presets`, `seed` | Source-controlled preset and bootstrap definitions |
| `ops` | Staging PostgreSQL backup source and systemd units |
| `jianweirs` | Existing remote-sensing source; its separate business integration remains outside this upload task |

## Configuration and security

Deployment credentials, API keys, passwords, environment files, private operational evidence, databases, uploaded business files and server backups are not part of this export. Configure credentials through the deployment environment or the existing private secret mechanism.

This repository starts with a snapshot commit so earlier server Git history is not transferred. Existing maintained frontend build assets are included where they are part of the staging source workspace. Keep source and build assets consistent when changing them.

This upload does not deploy or restart services. Production deployment remains a separate action.

## Export checks

Gitleaks 8.30.1 and additional credential-pattern checks were used. Obsolete backup copies and generated build-environment metadata were excluded after a fixed workbench token was detected there. The remaining scanner matches were reviewed as test fixtures or non-secret expressions. No prior Git history is included.
