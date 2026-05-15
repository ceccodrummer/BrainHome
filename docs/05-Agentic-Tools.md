# Capitolo 5: Agentic Tools (Scrittura e Azione)

## Obiettivo
Permettere all'IA di eseguire azioni controllate sul disco locale, come scrivere file e generare documentazione.

## 5.1 FastAPI File-Tool

API protetta per le operazioni su file:

- `POST /write`: scrive il codice generato dall'IA in un file specifico.
- `GET /read`: legge file non ancora indicizzati.
- `POST /git-commit`: esegue commit automatico della documentazione e delle modifiche generate.

## 5.2 Sicurezza

- Limiti di accesso a uno spazio di lavoro dedicato.
- Esecuzione dell'agente in una cartella isolata o chroot.
- Nessun accesso diretto ai file di sistema dell'host.
- Validazione dei percorsi e protezione da directory traversal.

## 5.3 Policy operative

- Definire i permessi minimi necessari.
- Separare i file di input/output del tool dai file di sistema.
- Registrare un audit log di tutte le azioni eseguite.

## 5.4 Checklist di implementazione

5.4.1 [x] Definire le API esposte dal tool agentico. (GET /health, POST /write, GET /read, POST /git-commit)
5.4.2 [x] Definire le operazioni autorizzate (`read`, `write`, `git-commit`). (implementate in services/agent-tools/app.py)
5.4.3 [x] Definire l'ambito di filesystem consentito. (WORKSPACE_DIR=/workspace, montato su data/agent-workspace)
5.4.4 [x] Definire le regole di validazione dei percorsi. (_resolve_safe_path() con Path.relative_to() anti directory-traversal)
5.4.5 [x] Definire il modello di autorizzazione per le richieste. (Bearer token via AGENT_TOKEN; disabilitabile per test locali)
5.4.6 [x] Definire le policy di logging e audit. (audit_logger → /app/audit.log con ogni operazione WRITE/READ/GIT_COMMIT/BLOCKED)
5.4.7 [ ] Definire la procedura di rollback in caso di errore. *(rimandato — git revert manuale per ora)*
5.4.8 [x] Definire la policy Git per commit automatici. (GIT_AUTHOR_NAME/EMAIL da env, git add + commit con messaggio custom)
5.4.9 [ ] Definire la gestione dei conflitti e delle revisioni. *(rimandato — fuori scope MVP)*
5.4.10 [x] Definire il controllo dell'accesso per gli agenti. (Bearer token + path containment + max file size 512KB)
5.4.11 [x] Aggiungere tool-dispatch in dify_stub: rileva intento (write/read/commit) e chiama agent-tools direttamente.
5.4.12 [x] Aggiungere endpoint GET /list per elencare file nel workspace.
5.4.13 [x] Aggiungere endpoint DELETE /delete per cancellare file dal workspace.
5.4.14 [x] Aggiungere endpoint POST /append per appendere contenuto a un file esistente.
5.4.15 [x] Aggiornare intent detection per supportare list/delete/append.
5.4.16 [x] Aggiungere endpoint GET /tree — albero ricorsivo del workspace (depth max configurabile).
5.4.17 [x] Aggiungere endpoint POST /search — ricerca testo/regex nei file del workspace.
5.4.18 [x] Aggiungere endpoint POST /move — sposta/rinomina file nel workspace.
5.4.19 [x] Aggiungere endpoint POST /run — esecuzione sandboxed di file .py con timeout e cattura stdout/stderr.
5.4.20 [x] Multi-intent chaining in dify_stub — un singolo messaggio può eseguire più tool in sequenza (es. "crea X, poi aggiungi Y").
5.4.21 [x] Workspace context injection — il file tree viene iniettato nel prompt LLM per le query non-tool, così il modello conosce i file esistenti.
5.4.22 [x] Rendering markdown nei bubble chat (code blocks, bold, inline code, liste).
5.4.23 [x] Tool badge nel bubble dell'assistente — mostra visivamente quale strumento è stato usato.

## 5.5 Attività principali

- Progettare l'API FastAPI per le azioni agentiche.
- Progettare il perimetro di sicurezza del tool.
- Progettare il logging e il rollback.
- Progettare la policy Git per le modifiche automatiche.

## 5.6 Punti da definire

- Flusso di approvazione umano prima della scrittura definitiva.
- Rollback automatico in caso di errore.
- Policy di commit e branch Git per i cambiamenti generati.
- Gestione dei conflitti Git e delle revisioni.
