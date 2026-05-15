# Capitolo 6: Hardware & Scalabilità

## Obiettivo
Definire l'hardware di base e la strategia di crescita per sostenere Dify, il Watcher e gli agentic tools.

## 6.1 Raccomandazioni hardware

- CPU: Intel i7/i9 (12a gen o successiva) o equivalente.
- RAM: 32 GB minimo.
- Storage: 1 TB NVMe SSD.
- Opzioni consigliate:
  - Geekom
  - Beelink
  - Minisforum

### 6.1.1 Installazione su Mini PC Linux

- Il Mini PC Linux è il target finale per l'ambiente produttivo.
- Installare Ubuntu Server 24.04 LTS o Debian 12 sul Mini PC.
- Creare un utente non-root dedicato per i servizi container.
- Installare Docker Engine e Docker Compose V2 sul Mini PC.
- Configurare Tailscale sul Mini PC e collegarlo alla rete Tailscale esistente.
- Definire i volumi locali persistenti e le relative autorizzazioni.

## 6.2 Scalabilità

- Spostare l'inferenza LLM su una GPU esterna o su API cloud quando l'analisi diventa troppo pesante.
- Mantenere LiteLLM come layer di astrazione per evitare modifiche alla logica dell'agente.
- Valutare la separazione dei servizi su più nodi se necessario.

## 6.3 Operatività

- Verificare il carico CPU/RAM durante l'esecuzione simultanea di:
  - Dify
  - Vector DB
  - Watcher
  - LiveKit/whisper
  - FastAPI agentic tool

## 6.4 Checklist di implementazione

6.4.1 [ ] Definire i requisiti minimi hardware per il nodo principale.
6.4.2 [ ] Definire la configurazione storage e I/O richiesta.
6.4.3 [ ] Definire il requisito RAM per l'esecuzione simultanea dei servizi.
6.4.4 [ ] Definire la possibile strada per GPU locale o GPU remota.
6.4.5 [ ] Definire i limiti di carico attesi per i servizi core.
6.4.6 [ ] Definire la strategia di monitoraggio delle risorse.
6.4.7 [ ] Definire le opzioni di ridondanza e backup.
6.4.8 [ ] Definire la strategia di failover per i servizi critici.
6.4.9 [ ] Definire la roadmap di aggiornamento hardware.

## 6.5 Attività principali

- Progettare l'hardware del nodo centrale.
- Progettare la scalabilità verticale e orizzontale.
- Progettare il monitoraggio delle risorse.
- Progettare le opzioni di backup e failover.

## 6.6 Punti da definire

- Politiche di ridondanza e backup.
- Strategie di failover per i servizi critici.
- Requisiti per GPU locale vs GPU remota.
- Pianificazione degli aggiornamenti hardware.
