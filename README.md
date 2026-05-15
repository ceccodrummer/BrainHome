# BrainHome — Ecosistema AI Personale

Assistente IA personale, modulare e orientato allo sviluppo software, eseguito interamente su infrastruttura domestica. Tutti i servizi girano come container Docker e comunicano su una rete VPN privata (Tailscale).

---

## Architettura

```
Smartphone / Client
       │  (Tailscale VPN)
       ▼
 ┌─────────────┐     ┌───────────────┐     ┌───────────────┐
 │   FastAPI   │────▶│     Dify      │────▶│  LiteLLM /    │
 │  (porta 8000)│    │  (porta 3001) │     │  Ollama / Cloud│
 └─────────────┘     └───────────────┘     └───────────────┘
        │                    │
        ▼                    ▼
 ┌─────────────┐     ┌───────────────┐
 │ Agent Tools │     │   PostgreSQL  │
 │ (porta 8001)│     │  (Vector DB)  │
 └─────────────┘     └───────────────┘
        ▲
 ┌─────────────┐
 │   Watcher   │  (monitora file e sincronizza)
 └─────────────┘
```

### Servizi Docker

| Servizio | Porta | Descrizione |
|---|---|---|
| `dify` | 3001 | Orchestratore LLM (brain principale) |
| `fastapi` | 8000 | Interfaccia HTTP per input da mobile |
| `agent-tools` | 8001 | Strumenti agentici (lettura/scrittura file, azioni) |
| `watcher` | — | Monitora i file e sincronizza i dati con Dify |
| `postgres` | — | Database persistente per Dify |

---

## Prerequisiti

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) con WSL 2 abilitato
- [Tailscale](https://tailscale.com/) installato sull'host e sui client
- [Ollama](https://ollama.com/) (opzionale, per modelli locali) oppure credenziali OpenAI/Gemini

---

## Avvio rapido

```bash
# 1. Clona il repository
git clone https://github.com/ceccodrummer/BrainHome.git
cd BrainHome

# 2. Copia i template di configurazione e inserisci le tue credenziali
cp config/dify.env.example config/dify.env
cp config/fastapi.env.example config/fastapi.env
cp config/watcher.env.example config/watcher.env
cp config/agent-tools.env.example config/agent-tools.env

# 3. Configura il modello LLM in config.yaml

# 4. Avvia i container
docker compose up -d

# 5. Verifica che tutti i servizi siano attivi
docker compose ps
```

> I file `config/*.env` contengono credenziali e **non vengono tracciati da Git**.

---

## Struttura del progetto

```
BrainHome/
├── config.yaml              # Configurazione LiteLLM / modello LLM
├── docker-compose.yml       # Orchestrazione dei container
├── config/                  # Variabili d'ambiente per ogni servizio (escluse da Git)
├── data/
│   ├── agent-workspace/     # Workspace condiviso con l'agente
│   ├── dify/                # Knowledge base e configurazione Dify
│   ├── postgres/            # Volume dati PostgreSQL
│   └── whisper/             # Modelli Whisper (STT)
├── docs/                    # Documentazione architetturale per capitoli
├── scripts/                 # Script di utilità (firewall, versioning, test)
└── services/
    ├── agent-tools/         # FastAPI — strumenti agentici
    ├── dify_stub/           # Placeholder Dify (sviluppo locale)
    ├── fastapi/             # Interfaccia mobile (HTML + API)
    └── watcher/             # Servizio di sincronizzazione file
```

---

## Documentazione

| Capitolo | Argomento |
|---|---|
| [01 — Infrastruttura Core](docs/01-Infrastruttura-Core.md) | Docker, networking, Tailscale, volumi |
| [02 — Cervello (Dify)](docs/02-Cervello-Dify.md) | Orchestrazione LLM, knowledge base, agenti |
| [03 — Watcher](docs/03-Watcher-Sincronizzazione.md) | Sincronizzazione file e trigger automatici |
| [04 — Input Telefono](docs/04-Input-Telefono.md) | Interfaccia voce/testo da smartphone |
| [05 — Agentic Tools](docs/05-Agentic-Tools.md) | Strumenti di scrittura e azione dell'agente |
| [06 — Hardware & Scalabilità](docs/06-Hardware-Scalabilita.md) | Mini PC, risorse, espansione futura |

---

## Sicurezza

- I servizi sono esposti **esclusivamente sulla rete Tailscale** (nessuna porta aperta su Internet).
- Le credenziali risiedono in `config/*.env`, esclusi dal version control.
- Lo script `scripts/set-firewall-rules.ps1` configura le regole firewall di Windows.

---

*Ultimo aggiornamento: Maggio 2026*
