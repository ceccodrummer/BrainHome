# Capitolo 7: Action Agente -> Agente

## Obiettivo
Introdurre una action strutturata, sicura e osservabile che permetta a un agente di scrivere a un altro agente quando:

- il destinatario e piu pertinente per il task;
- la delega viene richiesta esplicitamente dall'utente;
- il flusso richiede coordinamento tra specializzazioni diverse.

## 7.1 Stato attuale

- `services/fastapi/main.py` mantiene il registry degli agenti e fa proxy verso `/query` e `/query/stream`.
- `services/dify_stub/app.py` esegue il loop agentico, abilita i tool e gestisce la sessione locale del singolo agente.
- `services/dify_stub/tool_executor.py` espone i tool al modello e li dispatcha verso servizi HTTP interni.
- `services/agent-tools/app.py` gestisce solo file, script e Git.
- Esiste un forwarding user -> agente basato su parsing testuale, ma non esiste una action nativa agente -> agente.

## 7.2 Principi di progetto

- La comunicazione tra agenti deve essere una action strutturata, non solo testo nel prompt.
- Il broker della comunicazione deve stare nel layer che conosce il registry degli agenti.
- L'identita del mittente deve essere esplicita e verificabile.
- La delega deve essere governata da policy, audit, timeout e anti-loop.
- Le sessioni agente -> agente devono essere separate dalle sessioni utente.
- Il sistema deve supportare sia delega autonoma sia delega esplicita richiesta dall'utente.

## 7.3 Architettura target

### Componenti

- `dify_stub`: espone al modello il nuovo tool `message_agent`.
- `fastapi`: agisce come broker interno della comunicazione tra agenti.
- `dify/agent-*`: ricevono la richiesta tramite i normali endpoint `/query` o `/query/stream`.
- `agent-tools`: resta focalizzato su filesystem/script/git e non va usato come broker di messaggistica.

### Flusso logico

1. L'agente A decide o riceve richiesta di coinvolgere l'agente B.
2. Il modello chiama il tool `message_agent`.
3. `tool_executor.py` invia una richiesta strutturata a un endpoint interno FastAPI.
4. FastAPI valida mittente, destinatario, policy, hop count e timeout.
5. FastAPI inoltra il messaggio all'agente B con una sessione dedicata.
6. La risposta dell'agente B torna a FastAPI.
7. FastAPI restituisce il risultato del tool all'agente A.
8. L'agente A integra la risposta nel proprio ragionamento e produce l'output finale.

## 7.4 Contratto dell'action

### Nome tool consigliato

- `message_agent`

### Payload minimo consigliato

```json
{
  "to_agent_id": "agent-3",
  "message": "Analizza mobile.js e proponi una patch per il bug X",
  "reason": "frontend specialist",
  "mode": "ask",
  "await_response": true
}
```

### Campi runtime da aggiungere lato backend

```json
{
  "from_agent_id": "agent-2",
  "trace_id": "uuid",
  "conversation_id": "uuid",
  "hop_count": 1,
  "max_hops": 3,
  "visited_agents": ["agent-2"]
}
```

### Risposta minima consigliata

```json
{
  "status": "ok",
  "target_agent_id": "agent-3",
  "trace_id": "uuid",
  "answer": "....",
  "latency_ms": 1840
}
```

## 7.5 Modalita supportate

- `ask`: il mittente chiede un contributo e attende una risposta testuale.
- `delegate`: il mittente assegna un sottotask e attende un risultato operativo.
- `notify`: il mittente invia un'informazione senza attendere risposta.

## 7.6 Ordine di sviluppo

### Fase 1 - Fondazioni e configurazione

7.6.1 [x] Definire l'identita runtime di ogni agente con `AGENT_ID`. (`docker-compose.yml`, `docker-compose.agents.yml`, `config/dify.env`)
7.6.2 [x] Definire opzionalmente `AGENT_NAME` e `AGENT_ROLE` per logging e policy. (`docker-compose.yml`, `docker-compose.agents.yml`, `config/dify.env`)
7.6.3 [x] Aggiornare `docker-compose.yml` e `docker-compose.agents.yml` per valorizzare `AGENT_ID` su ogni container agente. (mount condiviso di `/config` incluso)
7.6.4 [x] Stabilire una source of truth unica del registry agenti in `AGENTS_CONFIG`. (`config/agents.json` come registry condiviso; `AGENTS_CONFIG` resta fallback)
7.6.5 [x] Validare all'avvio che ogni `AGENT_ID` runtime esista nel registry. (`services/dify_stub/app.py`, `services/fastapi/main.py`)
7.6.6 [x] Definire un token interno dedicato per la comunicazione agente -> broker, separabile da `AGENT_TOOLS_TOKEN`. (`AGENT_BROKER_TOKEN` in `config/dify.env` e `config/fastapi.env`)
7.6.7 [x] Documentare env vars, default e fallback in `config/*.env`. (`AGENTS_CONFIG_PATH`, `AGENT_BROKER_TOKEN`, fallback `AGENTS_CONFIG`)

### Fase 2 - Contratto API e modelli dati

7.6.8 [x] Definire il payload ufficiale del tool `message_agent`. (`AgentMessageRequest` in `services/fastapi/main.py`, `MESSAGE_AGENT_TOOL_SCHEMA` in `services/dify_stub/tool_executor.py`)
7.6.9 [x] Definire i campi obbligatori e opzionali per ogni `mode`. (`mode`, `await_response`, `reason`, `protocol_version` validati in `AgentMessageRequest`)
7.6.10 [x] Definire il formato standard della risposta. (`AgentMessageResponse` in `services/fastapi/main.py`)
7.6.11 [x] Definire gli errori strutturati: `unauthorized`, `unknown_target`, `policy_denied`, `timeout`, `loop_blocked`, `invalid_payload`. (`AgentMessageError`, `AgentMessageErrorCode`, helper `_agent_message_error_response`)
7.6.12 [x] Definire `trace_id`, `conversation_id`, `hop_count`, `max_hops`, `visited_agents`. (`AgentMessageEnvelope` in `services/fastapi/main.py`)
7.6.13 [x] Definire limiti di dimensione per `message` e `answer`. (`AGENT_MESSAGE_*` constants e limiti esposti anche in `/health`)

### Fase 3 - Broker interno FastAPI

7.6.14 [x] Creare un endpoint interno dedicato, ad esempio `POST /internal/agent-message`. (`services/fastapi/main.py`)
7.6.15 [x] Proteggere l'endpoint con autenticazione bearer interna. (`_verify_agent_broker_token`, `AGENT_BROKER_TOKEN`)
7.6.16 [x] Validare che `from_agent_id` corrisponda all'identita reale del chiamante o al token associato. (match tra payload e header `X-Agent-Id`; auth interna condivisa)
7.6.17 [x] Validare che `to_agent_id` esista nel registry. (`_find_agent_by_id` + broker validation)
7.6.18 [x] Bloccare richieste verso se stessi salvo flag esplicito e motivato. (self-message attualmente negato dal broker)
7.6.19 [x] Applicare timeout di chiamata verso l'agente destinatario. (`AGENT_BROKER_TIMEOUT_SECONDS`)
7.6.20 [x] Restituire errori coerenti e leggibili dal tool caller. (`AgentMessageResponse` strutturato + parsing in `tool_executor.py`)

### Fase 4 - Policy e autorizzazioni

7.6.21 [ ] Introdurre una allowlist source -> target.
7.6.22 [ ] Definire se tutti gli agenti possono parlare con tutti o solo con alcuni.
7.6.23 [ ] Definire policy per `mode`: chi puo fare `notify`, `ask`, `delegate`.
7.6.24 [ ] Definire policy per `await_response`.
7.6.25 [ ] Definire policy per escalation da utente: se l'utente ordina una delega, quali vincoli restano comunque attivi.
7.6.26 [ ] Definire se il broker puo riscrivere o arricchire il prompt inoltrato.
7.6.27 [ ] Definire se alcuni agenti sono read-only o execution-capable.

### Fase 5 - Anti-loop e robustezza conversazionale

7.6.28 [ ] Introdurre `hop_count` con incremento obbligatorio a ogni inoltro.
7.6.29 [ ] Introdurre `max_hops` configurabile con default conservativo.
7.6.30 [ ] Introdurre `visited_agents` per bloccare cicli A -> B -> A.
7.6.31 [ ] Bloccare recursions profonde o topologie anomale.
7.6.32 [ ] Definire comportamento in caso di loop bloccato: errore tool chiaro, non silent fail.
7.6.33 [ ] Definire limiti sul numero di deleghe per singola richiesta utente.

### Fase 6 - Sessioni e isolamento del contesto

7.6.34 [ ] Non riusare la sessione utente originale per i messaggi agente -> agente.
7.6.35 [ ] Generare `session_id` dedicate tipo `agentlink:{conversation_id}:{from}:{to}`.
7.6.36 [ ] Definire durata e lifecycle di queste sessioni.
7.6.37 [ ] Definire se il destinatario deve vedere solo il messaggio corrente o anche un breve contesto sintetico.
7.6.38 [ ] Evitare leakage di cronologia utente non necessaria.
7.6.39 [ ] Definire se e come il destinatario puo continuare una sub-conversation multi-turn.

### Fase 7 - Tool schema nel dify stub

7.6.40 [x] Aggiungere `message_agent` a `TOOL_SCHEMAS` in `services/dify_stub/tool_executor.py`.
7.6.41 [x] Scrivere una descrizione chiara del tool per il modello. (`MESSAGE_AGENT_TOOL_SCHEMA` + `SYSTEM_PROMPT`)
7.6.42 [x] Definire parametri minimali per evitare ambiguita. (`to_agent_id`, `message`, `reason`, `mode`, `await_response`, `protocol_version`)
7.6.43 [x] Definire esempi di uso nel prompt o nella documentazione tecnica. (`AGENTI DISPONIBILI PER DELEGA` e istruzioni dedicate in `services/dify_stub/app.py`)
7.6.44 [x] Aggiornare l'executor per dispatchare il tool al broker FastAPI invece che ad `agent-tools`. (`services/dify_stub/tool_executor.py`)
7.6.45 [x] Gestire preview e truncation delle risposte del destinatario. (limite `AGENT_MESSAGE_ANSWER_MAX_CHARS` lato broker; formatting compatto lato executor)

### Fase 8 - Integrazione con il loop agentico

7.6.46 [x] Verificare che `_needs_tools()` abiliti i tool anche per richieste di delega tra agenti. (`_is_agent_routing_intent()` + `_needs_tools()` in `services/dify_stub/app.py`)
7.6.47 [x] Estendere le keyword per includere termini come `agente`, `inoltra`, `chiedi a`, `delegare`, `frontend`, `devops`. (`_AGENT_ROUTING_KEYWORDS`)
7.6.48 [x] Valutare se, con piu agenti registrati, convenga abilitare sempre i tool per ridurre falsi negativi. (scelta implementata: no always-on; detection estesa con mention, `id`, `name`, routing intent)
7.6.49 [x] Definire come il tool result viene re-iniettato nel contesto dell'agente chiamante. (nessuna modifica necessaria: il loop gia usa `_append_tool_result()` per reiniezione nel contesto)
7.6.50 [x] Verificare che un fallimento del destinatario non corrompa il loop complessivo. (errori tool ritornano come testo strutturato e il loop prosegue nel normale ciclo di reasoning)

### Fase 9 - UX e supporto alla delega esplicita utente

7.6.51 [x] Mantenere compatibile il forwarding diretto gia presente nella UI. (nessuna regressione nel routing esistente di `mobile.js` / `/proxy`)
7.6.52 [x] Distinguere nel backend i casi user -> target diretto dai casi user -> agente A -> agente B. (`delegations_used` esposto da `services/dify_stub/app.py` solo quando interviene `message_agent`)
7.6.53 [x] Valutare se mostrare in UI che una risposta e stata delegata. (badge e dettagli delega in `services/fastapi/static/mobile.js`)
7.6.54 [x] Valutare badge o meta-info come `coinvolto: Frontend`. (`delegation-badge`, meta `Delega: N`)
7.6.55 [x] Definire come mostrare errori di delega senza confondere l'utente finale. (badge errore dedicato + dettagli con `error_code`/`error_message`)

### Fase 10 - Logging, audit e osservabilita

7.6.56 [ ] Loggare ogni richiesta con `trace_id`, `from_agent_id`, `to_agent_id`, `mode`, `latency_ms`, esito.
7.6.57 [ ] Aggiungere audit log dedicato per le deleghe.
7.6.58 [ ] Loggare una preview limitata del messaggio evitando dump eccessivi.
7.6.59 [ ] Definire metriche minime: count richieste, success rate, timeout, policy denied, loop blocked.
7.6.60 [ ] Valutare metriche per agente: richieste ricevute, richieste delegate, latenza media.

### Fase 11 - Test e validazione

7.6.61 [ ] Testare chiamata valida `agent-2` -> `agent-3`.
7.6.62 [ ] Testare destinatario inesistente.
7.6.63 [ ] Testare policy deny.
7.6.64 [ ] Testare self-message bloccato.
7.6.65 [ ] Testare timeout del destinatario.
7.6.66 [ ] Testare loop A -> B -> A bloccato.
7.6.67 [ ] Testare `notify` senza risposta.
7.6.68 [ ] Testare `delegate` con risposta lunga e truncation.
7.6.69 [ ] Testare session isolation tra utente e sub-conversation.
7.6.70 [ ] Testare compatibilita con streaming e non-streaming.

### Fase 12 - Hardening finale

7.6.71 [ ] Introdurre rate limit per evitare tempeste di deleghe.
7.6.72 [ ] Introdurre circuit breaker temporaneo verso agenti non raggiungibili.
7.6.73 [ ] Definire fallback se il target e down: errore esplicito o retry limitato.
7.6.74 [ ] Definire limiti di concorrenza per deleghe parallele.
7.6.75 [ ] Verificare che i log non espongano segreti o payload sensibili.
7.6.76 [ ] Documentare procedura di troubleshooting operativa.

## 7.7 Punti aggiuntivi da non trascurare

- Separazione delle responsabilita: la messaggistica tra agenti non deve inquinare il perimetro di `agent-tools`.
- Compatibilita futura con agenti esterni: il broker deve poter crescere anche se un domani alcuni agenti non sono container locali.
- Governance del prompt: il broker non deve alterare liberamente la semantica del messaggio senza tracciarlo.
- Idempotenza: se una chiamata viene ritentata, va possibile riconoscere la stessa `trace_id`.
- Evoluzione schema: prevedere una versione payload per evitare rotture future.
- Sicurezza dei contenuti: limitare prompt injection tra agenti, soprattutto se uno dei due riceve input non trusted.
- Ownership del risultato: definire chiaramente che l'agente chiamante resta responsabile della risposta finale all'utente.

## 7.8 MVP consigliato

- Aggiungere `AGENT_ID` a tutti gli agenti.
- Introdurre `message_agent` con `mode=ask` e `await_response=true`.
- Implementare `POST /internal/agent-message` in FastAPI.
- Usare policy semplice iniziale all-to-all con self-message bloccato.
- Introdurre `trace_id`, `hop_count=1`, `max_hops=3`, `visited_agents`.
- Usare sessioni dedicate agente -> agente.
- Loggare ogni delega.
- Testare un caso base `DevOps -> Frontend`.

## 7.9 Post-MVP

- Supporto a `notify`.
- Supporto a `delegate` con policy differenziate.
- Badge UI sulla delega.
- Metriche e dashboard.
- Rate limiting e circuit breaker.
- Eventuale coda asincrona per richieste lente.

## 7.10 Decisione architetturale raccomandata

- Broker della messaggistica in `fastapi`.
- Tool schema e dispatch in `dify_stub`.
- Nessuna logica agente -> agente dentro `agent-tools`.
- Policy, tracing, session isolation e anti-loop obbligatori gia dalla prima implementazione utile.
