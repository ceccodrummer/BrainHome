# Capitolo 3: Il Sistema di Sincronizzazione (The Watcher)

## Obiettivo
Monitorare il file system e aggiornare automaticamente i vettori nella KB di Dify.

## 3.1 Architettura del Watcher

- Servizio Python che osserva le modifiche sui file.
- Libreria proposta: `watchdog`.
- Trigger principali:
  - `on_modified`
  - `on_created`
  - `on_deleted`

## 3.2 Pipeline di aggiornamento

1. Rilevare l'evento sul file system.
2. Leggere il contenuto del file modificato.
3. Suddividere il documento in chunk.
4. Chiamare l'endpoint API di Dify.
5. Aggiornare il documento corrispondente nella KB.

## 3.3 Chunking per codice

- Il codice richiede chunk basati su struttura logica.
- Utilizzare parser come `tree-sitter` per identificare:
  - classi
  - metodi
  - sezioni di configurazione
- File di configurazione (`.env`, `config.json`) trattati come singole unità.

## 3.4 Gestione delle eccezioni

- Ignorare file temporanei e file di lock.
- Evitare loop infiniti su modifiche generate dallo stesso watcher.
- Loggare errori e notificare eventuali fallimenti di indicizzazione.

## 3.5 Checklis di implementazione

3.5.1 [x] Definire la lista delle directory da monitorare. (WATCH_DIRS in config/watcher.env: /watch/data, /watch/services)
3.5.2 [x] Definire i tipi di file da indicizzare e quelli da ignorare. (INDEXABLE_EXTENSIONS + IGNORE_PATTERNS in watcher/app.py)
3.5.3 [x] Definire la mappatura tra file system e documenti Dify. (_derive_kb_id() mappa path → kb_id)
3.5.4 [x] Definire la logica di chunking per testo e codice. (_chunk_text() con split su paragrafi poi righe, max CHUNK_SIZE=1500 chars)
3.5.5 [x] Definire i parser da utilizzare per il codice (es. tree-sitter). (baseline word-split; tree-sitter rimandato a versione futura)
3.5.6 [x] Definire il comportamento per file creati, modificati, rinominati e cancellati. (on_created/on_modified → upsert; on_deleted/on_moved → delete+upsert)
3.5.7 [x] Definire le regole per il trattamento dei file di configurazione. (estensioni .env/.yaml/.toml/.ini indicizzate come chunk singolo se ≤ CHUNK_SIZE)
3.5.8 [x] Definire i meccanismi di fallback per chunk troppo grandi. (_chunk_text() split ricorsivo su paragrafi e righe)
3.5.9 [x] Definire i parametri di autenticazione per le API Dify. (DIFY_URL in config/watcher.env; auth token rimandato quando Dify reale sarà attivo)
3.5.10 [x] Definire la gestione degli errori e dei retry. (RETRY_MAX=3, RETRY_DELAY=2s in _push_to_dify() + audit log)

## 3.6 Attività principali

- Progettare il watcher Python e le regole di monitoraggio.
- Progettare il flusso di sincronizzazione verso Dify.
- Progettare il chunking specifico per codice e testo.
- Progettare la gestione delle eccezioni e l'audit log.

## 3.7 Punti da definire

- Mappatura tra file system e documenti Dify.
- Politica di aggiornamento per file cancellati o rinominati.
- Supporto per formati non testuali (diagrammi, immagini).
- Limiti di dimensione per i chunk e strategie di fallback.
- Autenticazione e sicurezza delle chiamate API a Dify.
