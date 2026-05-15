# Capitolo 4: Interfaccia di Input (Voce e Testo dal Telefono)

## Obiettivo
Creare un ponte tra l'utente mobile e il server domestico, con input testuale e vocale.

## 4.1 Modulo light: Telegram

- Bot Python che riceve messaggi e note vocali.
- Inoltro dell'audio a un container Whisper per trascrizione.
- Invio del testo trascritto a Dify.
- Conversione della risposta di Dify in audio (TTS) e invio come nota vocale.

## 4.2 Modulo avanzato: WebRTC

- Streaming audio bidirezionale a bassa latenza con LiveKit Agents.
- Obiettivo: latenza inferiore a `500 ms`.
- Possibile utilizzo per conversazioni vocali continue.

## 4.3 Note tecniche

- VAD (Voice Activity Detection) per gestire i turni di parola.
- Qualità audio e compressione compatibile con il server.
- Autenticazione lato smartphone.

## 4.4 Checklist di implementazione

4.4.1 [x] Definire l'interfaccia utente minimale per l'accesso mobile.
4.4.2 [x] Definire i canali di input supportati (testo, audio, note vocali). (testo via web UI attivo; audio/Whisper/Telegram rimandato)
4.4.3 [ ] Definire il flusso di trascrizione audio con Whisper. *(rimandato — fuori scope MVP)*
4.4.4 [ ] Definire il flusso di ritorno audio/TTS. *(rimandato — fuori scope MVP)*
4.4.5 [ ] Definire il supporto WebRTC con LiveKit se previsto. *(rimandato — fuori scope MVP)*
4.4.6 [x] Definire le regole di autenticazione e autorizzazione. (accesso via Tailscale IP; auth token rimandato a versione futura)
4.4.7 [x] Definire la gestione degli errori lato client. (mobile.js: errori HTTP, JSON parse error, messaggi utente leggibili)
4.4.8 [x] Definire la strategia di riconnessione e retry. (fetchWithRetry con RETRY_MAX=3, RETRY_DELAY=1.5s; health check ogni 30s)
4.4.9 [x] Definire le metriche da monitorare per latenza e qualità. (latency_ms + kb_used visualizzati sotto ogni risposta dell'IA)
4.4.10 [x] Creare un’interfaccia web mobile minimale per testare il sistema dal browser dello smartphone.
4.4.11 [x] Testare l'accesso via Tailscale usando l'indirizzo IP del nodo.
4.4.12 [x] Verificare l'interfaccia mobile minimale su `http://100.87.153.12:8000/`.
4.4.13 [x] Refactoring frontend in file separati: `templates/index.html`, `static/mobile.css`, `static/mobile.js`.
4.4.14 [x] Trasformare la UI in chat stile WhatsApp (bolle utente a destra, IA a sinistra).
4.4.15 [x] Implementare header fisso (verde) e input-toolbar fisso in fondo alla pagina.
4.4.16 [x] Aggiungere endpoint `/health` per il controllo stato del backend.
4.4.17 [x] Mostrare lo stato della connessione backend nell'header (testo bianco).
4.4.18 [x] Aggiungere tracking versione frontend (`v1.0.3`) con placeholder `{{version}}` nel template.
4.4.19 [x] Creare script `scripts/bump-frontend-version.py` per incremento automatico della versione patch.
4.4.20 [x] Creare git hook pre-commit (`.githooks/pre-commit`) per auto-bump versione al commit.
4.4.21 [x] Verificare il flusso end-to-end Q&A dallo smartphone via Tailscale.
4.4.22 [x] Aggiungere sessione persistente lato client (sessionStorage → session_id inviato al backend).
4.4.23 [x] Bump versione frontend a v1.0.4 per le modifiche js/css.
4.4.24 [x] Sostituire sessionStorage con localStorage per persistenza sessione tra chiusure del tab.
4.4.25 [x] Persistere le sessioni su disco (sessions.json) nel backend dify_stub per sopravvivere ai restart.
## 4.5 Attività principali

- Progettare il modulo Telegram per input vocale e testuale.
- Progettare il modulo WebRTC per streaming audio bidirezionale.
- Progettare l'interazione tra dispositivo mobile e server domestico.
- Progettare la gestione della connessione e dei retry.

## 4.6 Punti da definire

- Progetto dell'app lato smartphone.
- Gestione delle credenziali e del login.
- Strategie di riconnessione e retry.
- Metriche di latenza e qualità.
- Compatibilità con reti mobili e Wi-Fi.
