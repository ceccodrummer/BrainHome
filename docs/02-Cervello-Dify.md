# Capitolo 2: Il Cervello (Orchestrare con Dify)

## Obiettivo
Usare Dify come orchestratore RAG e interfaccia centrale per la Knowledge Base e gli agenti.

## 2.1 Architettura Knowledge Base (KB)

- Struttura basata su più KB tematiche per mantenere il focus.
- Ogni progetto o repository ha una KB dedicata.
- Esempi:
  - `KB_Progetto_A` -> repository frontend
  - `KB_Progetto_B` -> repository backend

### Vantaggi
- Riduce il rumore di contesto.
- Migliora la qualità delle risposte.
- Permette ricerche mirate in base all'ambito.

## 2.2 Configurazione agente

- Definire il `system prompt` dell'agente:
  - Esempio: "Sei un Senior Software Architect con accesso ai file locali".
- Usare variabili di sessione per controllare l'ambito operativo.
- Mappare gli input dell'utente alla KB corretta.

## 2.3 Flusso RAG

- Ricezione della domanda.
- Selezione della KB pertinente.
- Recupero dei documenti più rilevanti.
- Costruzione del prompt con contesto e invio all'LLM.

## 2.4 Integrazione con LiteLLM

- LiteLLM funge da proxy verso il modello selezionato.
- Mantenere separazione tra:
  - logica di orchestrazione Dify
  - inferenza LLM

## 2.5 Checklist di implementazione

2.5.1 [x] Definire la struttura delle KB tematiche. (kb_sistema, kb_frontend, kb_ai in data/dify/)
2.5.2 [x] Definire la mappatura tra repository/progetti e KB. (data/dify/kb_config.json)
2.5.3 [x] Definire il `system prompt` principale dell'agente. (config/dify.env + dify_stub/app.py)
2.5.4 [x] Definire le variabili di sessione per l'ambito operativo. (SESSIONS in-memory con history e active_kb)
2.5.5 [x] Definire le regole di instradamento degli input verso la KB corretta. (routing_rules in kb_config.json + _route_question())
2.5.6 [x] Definire il flusso RAG: retrieval, prompt building e inferenza. (word-overlap retrieval + history context + Ollama)
2.5.7 [x] Definire l'integrazione di Dify con il proxy LiteLLM. (LITELLM_URL/LITELLM_MODEL in dify.env, POST /v1/completions)
2.5.8 [x] Definire i test di validazione della risposta sulla KB corretta. (scripts/test-kb-routing.py)
2.5.9 [x] Preparare i casi d'uso per le richieste multi-KB. (kb.json kb-4, script di test con 6 casi d'uso)

## 2.6 MVP minimo per avviare e provare

- [x] Creare una sola KB di test con contenuto essenziale.
- [x] Definire un `system prompt` semplice e mirato per l'agente.
- [x] Usare Ollama locale come provider LiteLLM (configurazione pronta).
- [x] Usare il proxy FastAPI/Dify placeholder per la prima integrazione.
- [x] Eseguire una query di prova e verificare il percorso di richiesta.
- [x] Verificare il flusso completo end-to-end: smartphone → FastAPI → Dify stub → Ollama → risposta.
- [ ] Posticipare ranking avanzato, gestione multi-KB e fallback. *(rimandato — focus su MVP)*

Strumento di test:

- `python scripts/test-dify-query.py "La mia domanda qui"`
- Apri il browser sullo smartphone e visita `http://100.87.153.12:8000/` per usare l'interfaccia mobile.

> Nota: la configurazione Ollama locale è pronta e il servizio è stato avviato con successo. La pipeline Dify->FastAPI->Ollama è stata testata e restituisce risposte, anche se alcune stringhe contengono caratteri accentati codificati come sequenze otto-bit.

## 2.7 Attività principali

- Progettare l'architettura delle Knowledge Base.
- Progettare la logica di routing delle query.
- Progettare il prompt engineering per l'agente.
- Progettare il controllo dello stato multi-turno.
- Progettare il fallback quando una KB non risponde.

## 2.8 Punti da definire

- Strategia di selezione e ranking dei documenti.
- Criteri di aggiornamento delle embedding.
- Gestione dello stato di sessione multi-turno.
- Fallback quando una KB non è disponibile.
- Policy di accesso tra agente e dati sensibili.
