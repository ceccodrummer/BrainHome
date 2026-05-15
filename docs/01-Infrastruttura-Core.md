# Capitolo 1: Infrastruttura Core (Docker & Networking)

## Obiettivo
Costruire un ambiente isolato, riproducibile e accessibile in sicurezza dall'esterno, utilizzando container Docker e una rete privata crittografata.

## 1.1 Virtualizzazione con Docker

- Tutti i servizi devono eseguire come container per garantire modularità.
- Utilizzare Docker Compose V2 per orchestrare:
  - Dify
  - Vector DB / embedding store
  - FastAPI agentic tool
  - LiveKit o altri servizi di input
  - Eventuali servizi di transcodifica audio

### LiteLLM
- Proxy per rendere l'LLM intercambiabile.
- Consente di passare da una sorgente all'altra senza modificare la logica dell'agente.
- Supporto previsto:
  - Gemini
  - OpenAI
  - modelli locali

## 1.2 Networking e accesso remoto

- Soluzione proposta: Tailscale VPN mesh.
- Ogni nodo (Mini PC, smartphone, workstation) acquisisce un IP privato.
- I servizi locali restano inacessibili da Internet diretto e sono esposti solo sulla rete VPN.
- Esempio di IP privato Tailscale: `100.64.0.5`.

## 1.3 Configurazione dei volumi e persistenza

- Creare cartelle locali per i volumi Docker, ad esempio:
  - `./data/dify`
  - `./data/postgres`
  - `./data/whisper`
  - `./data/watchdog`
- Definire permessi e ownership adeguati per l'utente Docker.

## 1.4 Checklist di implementazione

1.4.1 [x] Predisporre un ambiente Windows con Docker Desktop per il proof-of-concept iniziale.
1.4.2 [x] Installare Docker Desktop su Windows e abilitare il supporto WSL 2 se necessario.
1.4.3 [x] Installare Tailscale su Windows e smartphone.
1.4.4 [x] Collegare i dispositivi alla stessa rete Tailscale.
1.4.5 [x] Preparare il file `docker-compose.yml` con i servizi base.
1.4.6 [x] Definire i volumi di persistenza locali per Dify, database e servizi audio.
1.4.7 [x] Verificare la disponibilità e i permessi delle cartelle di volume.
1.4.8 [x] Preparare il file `config.yaml` di LiteLLM con le credenziali richieste.
1.4.9 [x] Avviare i container e verificare l'avvio dei servizi.
1.4.10 [x] Testare il proxy LiteLLM tramite chiamata locale.
1.4.11 [x] Verificare che i servizi siano raggiungibili solo via Tailscale.
1.4.12 [ ] Predisporre una policy firewall di base per porte e host.
1.4.13 [ ] Stabilire una procedura di backup e restore per i volumi locali.
1.4.14 [ ] Definire come aggiornare i container senza downtime.

## 1.5 Attività principali

- Definire l'architettura dei container e la loro rete interna.
- Definire quali volumi devono essere persistenti.
- Definire le dipendenze tra i servizi (Dify, Vector DB, LiteLLM, agentic tool).
- Definire i controlli di accesso e le restrizioni di rete.
- Definire i test di avvio e le verifiche di integrazione iniziale.

## 1.6 Note di implementazione

- Questo capitolo si concentra sull'installazione iniziale su PC Windows.
- Il servizio `dify` al momento è avviato con un placeholder locale perché l'immagine ufficiale Dify non è pubblicamente accessibile senza credenziali.
- Usare un bridge Docker interno per isolare i servizi.
- Usare Tailscale come gateway sicuro per i dispositivi esterni.
- Conservare le chiavi e le credenziali in file di configurazione non tracciati dal version control.
- Mantenere il setup ripetibile con una struttura di directory standard.
- Applicare regole firewall di base su Windows per limitare l'accesso alle porte esposte sulla tailnet.
- Usare `scripts/set-firewall-rules.ps1` come script di riferimento per creare le regole richieste.

## 1.7 Punti da definire

- Configurazione firewall del router/host.
- Regole dettagliate di sicurezza per i servizi Docker.
- Backup e restore dei volumi principali.
- Monitoraggio dello stato dei container.
- Modalità di aggiornamento dei container senza downtime.
