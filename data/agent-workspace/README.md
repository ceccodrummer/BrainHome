# Brain-Home Agent Workspace

Questa directory è il filesystem sandbox in cui il servizio `agent-tools` può leggere e scrivere file.

- Ogni file scritto dall'IA tramite `POST /write` atterrerà qui.
- `POST /git-commit` eseguirà commit dentro questa directory.
- Nessun accesso è consentito fuori da questa directory (path validation).
