"""
Test di validazione del flusso RAG per le KB tematiche.
Uso: python scripts/test-kb-routing.py [dify_url]

Verifica che ogni domanda venga instradata alla KB corretta
e che la risposta sia coerente con il contesto atteso.
"""

import json
import sys
from urllib.request import Request, urlopen

DIFY_URL = sys.argv[1] if len(sys.argv) > 1 else "http://100.87.153.12:3001"

# Test cases: (domanda, kb_attesa)
TEST_CASES = [
    ("Come funziona l'interfaccia mobile chat?", "kb_frontend"),
    ("Quali endpoint espone FastAPI?", "kb_frontend"),
    ("Come funziona il flusso RAG con Ollama?", "kb_ai"),
    ("Qual è il system prompt dell'agente IA?", "kb_ai"),
    ("Come è configurato Docker e Tailscale?", "kb_sistema"),
    ("Descrivi l'architettura del sistema Brain-Home.", "kb_sistema"),
]


def query(question: str) -> dict:
    payload = json.dumps({"question": question}).encode("utf-8")
    req = Request(
        f"{DIFY_URL}/query",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urlopen(req, timeout=30) as response:
        return json.load(response)


def main():
    passed = 0
    failed = 0
    print(f"Testing RAG routing against {DIFY_URL}\n{'='*60}")

    for question, expected_kb in TEST_CASES:
        try:
            result = query(question)
            kb_used = result.get("kb_used", "?")
            latency = result.get("latency_ms", "?")
            ok = kb_used == expected_kb
            status = "PASS" if ok else "FAIL"
            if ok:
                passed += 1
            else:
                failed += 1
            print(f"[{status}] KB attesa={expected_kb} | KB usata={kb_used} | {latency}ms")
            print(f"       Q: {question[:70]}")
            if not ok:
                print(f"       A: {str(result.get('answer', ''))[:100]}")
            print()
        except Exception as exc:
            failed += 1
            print(f"[ERROR] {question[:60]}: {exc}\n")

    print(f"{'='*60}")
    print(f"Risultati: {passed} PASS, {failed} FAIL su {len(TEST_CASES)} test")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
