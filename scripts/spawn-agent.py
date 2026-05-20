#!/usr/bin/env python3
"""
Brain-Home Agent Spawner
========================
Instantiates a new agent from the brainhome-dify:latest image template.

Usage:
  python scripts/spawn-agent.py --id 2 --port 3002 [options]
  python scripts/spawn-agent.py --id 2 --port 3002 --start
  python scripts/spawn-agent.py --list
  python scripts/spawn-agent.py --remove 2

Options:
  --id       INT     Unique numeric ID for the agent (required)
  --port     INT     Host port to expose (required, e.g. 3002 for agent 2)
  --name     STR     Short name label, e.g. "devops" (default: agent-{id})
  --prompt   STR     Full SYSTEM_PROMPT override (default: inherits from dify.env)
  --model    STR     LLM model override (default: inherits from dify.env)
  --start            Start the container after generation
  --stop             Stop and remove the agent container
  --remove   INT     Remove agent {id} config + data dirs (does NOT delete data/*)
  --list             List all spawned agents and their status

Output files:
  config/dify-{id}.env          — env-file for the agent container
  data/dify-{id}/               — persistent data (sessions, KB)
  data/agent-workspace-{id}/    — isolated file workspace (agent-tools)
  docker-compose.agents.yml     — Compose override with all spawned agents
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent.resolve()
BASE_ENV = ROOT / "config" / "dify.env"
AGENTS_COMPOSE = ROOT / "docker-compose.agents.yml"
AGENTS_REGISTRY = ROOT / "config" / "agents.json"
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"

# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_env_file(path: Path) -> dict:
    """Parse a .env file into a dict, ignoring comments and blank lines."""
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env


def _write_env_file(path: Path, env: dict, header: str = ""):
    """Write a dict back to a .env file."""
    lines = []
    if header:
        for h in header.splitlines():
            lines.append(f"# {h}" if not h.startswith("#") else h)
        lines.append("")
    for k, v in env.items():
        lines.append(f"{k}={v}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _read_agents_compose() -> str:
    if AGENTS_COMPOSE.exists():
        return AGENTS_COMPOSE.read_text(encoding="utf-8")
    return ""


def _slugify(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return cleaned or "agent"


def _load_agents_registry() -> list[dict]:
    if AGENTS_REGISTRY.exists():
        return json.loads(AGENTS_REGISTRY.read_text(encoding="utf-8"))
    return [{
        "id": "dify",
        "name": "Principale",
        "url": "http://dify:3000",
        "mention": "principale",
        "role": "primary",
    }]


def _save_agents_registry(agents: list[dict]):
    AGENTS_REGISTRY.write_text(json.dumps(agents, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _upsert_agent_registry(agent_id: int, name: str):
    registry = _load_agents_registry()
    runtime_id = f"agent-{agent_id}"
    mention = _slugify(name)
    record = {
        "id": runtime_id,
        "name": name,
        "url": f"http://{runtime_id}:3000",
        "mention": mention,
        "role": mention,
    }
    for idx, agent in enumerate(registry):
        if agent.get("id") == runtime_id:
            registry[idx] = record
            break
    else:
        registry.append(record)
    _save_agents_registry(registry)


def _remove_agent_registry(agent_id: int):
    runtime_id = f"agent-{agent_id}"
    registry = [agent for agent in _load_agents_registry() if agent.get("id") != runtime_id]
    _save_agents_registry(registry)


def _get_host_ip() -> str:
    """Extract bind IP from the base dify.env ports, or default."""
    # Read docker-compose.yml to extract the IP used for dify
    dc = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    m = re.search(r'"(\d+\.\d+\.\d+\.\d+):\d+:3000"', dc)
    return m.group(1) if m else "0.0.0.0"


def _service_block(agent_id: int, port: int, name: str, host_ip: str, system_prompt: str, model: str = "") -> str:
    """Return the YAML block for a single agent service.

    All agents share:
      - config/dify.env  (LLM key, model, temperature, …)
      - data/dify/       (KB files)
      - agent-tools      (file workspace)
    Per-agent overrides are injected via `environment:` to avoid duplicating secrets.
    """
    # Escape the prompt for YAML block scalar
    prompt_escaped = system_prompt.replace('"', '\\"')
    model_line = f'\n      LLM_MODEL: "{model}"' if model else ""
    role = _slugify(name)
    return f"""
  agent-{agent_id}:
    <<: *agent-base
    container_name: agent-{agent_id}
    env_file:
      - ./config/dify.env
    environment:
      AGENT_ID: "agent-{agent_id}"
      AGENT_NAME: "{name}"
      AGENT_ROLE: "{role}"
      SESSIONS_FILE: /app/data/sessions-{agent_id}.json{model_line}
      SYSTEM_PROMPT: "{prompt_escaped}"
    ports:
      - "{host_ip}:{port}:3000"
    volumes:
      - ./data/dify:/app/data
      - ./config:/config:ro
      - huggingface_cache:/root/.cache/huggingface
"""


def _upsert_service(agent_id: int, port: int, name: str, host_ip: str, system_prompt: str, model: str = ""):
    """Add or replace the agent-{id} service block in docker-compose.agents.yml."""
    block = _service_block(agent_id, port, name, host_ip, system_prompt, model)
    content = _read_agents_compose()

    if not content:
        # New file
        header = (
            "# Auto-generated by scripts/spawn-agent.py — do not edit manually.\n"
            "# Apply with: docker compose -f docker-compose.yml -f docker-compose.agents.yml up -d\n\n"
            "x-agent-base: &agent-base\n"
            "  image: brainhome-dify:latest\n"
            "  restart: unless-stopped\n"
            "  entrypoint: \"\"\n"
            "  command: uvicorn app:app --host 0.0.0.0 --port 3000\n"
            "  depends_on:\n"
            "    - postgres\n"
            "  networks:\n"
            "    - brainhome\n"
            "  volumes:\n"
            "    - huggingface_cache:/root/.cache/huggingface\n\n"
            "services:\n"
        )
        content = header + block
    else:
        # Replace existing block or append
        pattern = rf"\n  agent-{agent_id}:.*?(?=\n  [a-z]|\Z)"
        if re.search(pattern, content, re.DOTALL):
            content = re.sub(pattern, block.rstrip(), content, flags=re.DOTALL)
        else:
            # Insert before networks section
            if "\nnetworks:" in content:
                content = content.replace("\nnetworks:", block + "\nnetworks:", 1)
            else:
                content += block

    AGENTS_COMPOSE.write_text(content, encoding="utf-8")


def _remove_service(agent_id: int):
    """Remove agent-{id} block from docker-compose.agents.yml."""
    content = _read_agents_compose()
    if not content:
        return
    pattern = rf"\n  agent-{agent_id}:.*?(?=\n  [a-z]|\nnetworks:|\Z)"
    content = re.sub(pattern, "", content, flags=re.DOTALL)
    AGENTS_COMPOSE.write_text(content, encoding="utf-8")


def _docker_compose_cmd(*args) -> list:
    base = ["docker", "compose", "-f", str(ROOT / "docker-compose.yml")]
    if AGENTS_COMPOSE.exists():
        base += ["-f", str(AGENTS_COMPOSE)]
    return base + list(args)


# ── Commands ─────────────────────────────────────────────────────────────────

def cmd_spawn(args):
    agent_id = args.id
    port = args.port
    name = args.name or f"agent-{agent_id}"
    host_ip = _get_host_ip()

    # Determine SYSTEM_PROMPT
    if args.prompt:
        system_prompt = args.prompt
    else:
        base_prompt = _parse_env_file(BASE_ENV).get("SYSTEM_PROMPT", "")
        system_prompt = f"[Agente: {name}] {base_prompt}"

    model = args.model or ""

    # 1. No per-agent env file — config/dify.env is shared.
    #    Only SESSIONS_FILE + SYSTEM_PROMPT are overridden inline in docker-compose.

    # 2. Shared data dir (data/dify) — no per-agent dir needed.
    #    Just ensure it exists (created by the main dify service, but guard anyway).
    shared_data = DATA_DIR / "dify"
    shared_data.mkdir(parents=True, exist_ok=True)

    # 3. Upsert docker-compose.agents.yml
    _upsert_service(agent_id, port, name, host_ip, system_prompt, model)
    _upsert_agent_registry(agent_id, name)
    print(f"[+] docker-compose.agents.yml  (agent-{agent_id} → port {port})")
    print(f"[+] config/agents.json       (agent-{agent_id} registry upsert)")
    print(f"    env_file:     config/dify.env  (condiviso)")
    print(f"    data volume:  data/dify        (KB condivisa)")
    print(f"    sessions:     data/dify/sessions-{agent_id}.json")
    print(f"    workspace:    data/agent-workspace  (via agent-tools, condiviso)")

    print(f"\nAgente {agent_id} ({name}) configurato.")
    print(f"  URL:  http://{host_ip}:{port}/health")
    print(f"\nPer avviarlo:")
    print(f"  docker compose -f docker-compose.yml -f docker-compose.agents.yml up -d agent-{agent_id}")

    if args.start:
        print(f"\n[*] Avvio agent-{agent_id}...")
        subprocess.run(_docker_compose_cmd("up", "-d", f"agent-{agent_id}"), cwd=ROOT, check=True)


def cmd_stop(args):
    agent_id = args.id
    print(f"[*] Stop agent-{agent_id}...")
    subprocess.run(_docker_compose_cmd("stop", f"agent-{agent_id}"), cwd=ROOT)
    subprocess.run(_docker_compose_cmd("rm", "-f", f"agent-{agent_id}"), cwd=ROOT)


def cmd_remove(args):
    agent_id = args.remove
    # Stop container if running
    subprocess.run(
        ["docker", "stop", f"agent-{agent_id}"],
        cwd=ROOT, capture_output=True
    )
    subprocess.run(
        ["docker", "rm", f"agent-{agent_id}"],
        cwd=ROOT, capture_output=True
    )
    # Remove from compose file
    _remove_service(agent_id)
    _remove_agent_registry(agent_id)
    print(f"[-] agent-{agent_id} rimosso da docker-compose.agents.yml")
    print(f"[-] agent-{agent_id} rimosso da config/agents.json")
    sessions_file = DATA_DIR / "dify" / f"sessions-{agent_id}.json"
    if sessions_file.exists():
        print(f"    Nota: {sessions_file.relative_to(ROOT)} è ancora presente (dati sessione).")


def cmd_list(args):
    # Find agents from docker-compose.agents.yml
    content = _read_agents_compose()
    agent_ids = re.findall(r"\n  agent-(\d+):", content)
    if not agent_ids:
        print("Nessun agente spawned trovato.")
        return

    print(f"{'ID':<4} {'Prompt (preview)':<45} {'Port':<6} {'Status'}")
    print("-" * 70)
    for agent_id in sorted(agent_ids, key=int):
        # Extract SYSTEM_PROMPT from environment block in compose
        prompt_m = re.search(
            rf'agent-{agent_id}:.*?SYSTEM_PROMPT: "([^"]*)',
            content, re.DOTALL
        )
        prompt_preview = (prompt_m.group(1)[:42] + "...") if prompt_m else "(default)"

        # Check container status
        result = subprocess.run(
            ["docker", "inspect", f"agent-{agent_id}", "--format", "{{.State.Status}}"],
            capture_output=True, text=True
        )
        status = result.stdout.strip() if result.returncode == 0 else "not created"

        # Extract port from compose file (find port within the agent's own block)
        content = _read_agents_compose()
        port_m = re.search(
            rf'agent-{agent_id}:.*?"[\d.]+:(\d+):3000"', content, re.DOTALL
        )
        port = port_m.group(1) if port_m else "?"

        print(f"{agent_id:<4} {prompt_preview:<45} {port:<6} {status}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Brain-Home Agent Spawner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--id", type=int, help="Agent ID (e.g. 2)")
    parser.add_argument("--port", type=int, help="Host port (e.g. 3002)")
    parser.add_argument("--name", help="Short name label (e.g. devops)")
    parser.add_argument("--prompt", help="Full SYSTEM_PROMPT for this agent")
    parser.add_argument("--model", help="LLM model override")
    parser.add_argument("--start", action="store_true", help="Start container after generation")
    parser.add_argument("--stop", action="store_true", help="Stop agent container")
    parser.add_argument("--remove", type=int, metavar="ID", help="Remove agent config by ID")
    parser.add_argument("--list", action="store_true", help="List all spawned agents")

    args = parser.parse_args()

    if args.list:
        cmd_list(args)
    elif args.remove is not None:
        cmd_remove(args)
    elif args.stop and args.id:
        cmd_stop(args)
    elif args.id and args.port:
        cmd_spawn(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
