// ── Constants ──────────────────────────────────────────────────────────────
const RETRY_MAX = 3;
const RETRY_DELAY_MS = 1500;
const HEALTH_INTERVAL_MS = 30000;

// Persistent session id for multi-turn conversation (localStorage survives tab close)
let sessionId = localStorage.getItem('brainhome_session') || null;

// ── Status helpers ──────────────────────────────────────────────────────────
function setStatus(message, isError = false) {
  const statusEl = document.getElementById('status');
  if (!statusEl) {
    console.warn('Status element not found');
    return;
  }
  statusEl.textContent = message;
  statusEl.style.color = isError ? '#b00020' : '#5f6368';
}

function setBackendStatus(message, isHealthy = false) {
  const backendEl = document.getElementById('backendStatus');
  if (!backendEl) {
    console.warn('Backend status element not found');
    return;
  }
  backendEl.textContent = message;
  backendEl.style.color = '#ffffff';
}

// ── Health check with auto-reconnect  (4.4.8) ──────────────────────────────
async function checkBackendHealth() {
  setStatus('Verifica backend...', false);
  try {
    const res = await fetch('/health');
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}`);
    }
    const data = await res.json();
    if (data.status === 'ok') {
      setBackendStatus('Connesso', true);
      setStatus('Pronto.', false);
    } else {
      setBackendStatus('Non disponibile', false);
      setStatus('Backend non disponibile.', true);
    }
  } catch (err) {
    console.error('Health check failed:', err);
    setBackendStatus('Non raggiungibile', false);
    setStatus('Backend non raggiungibile. Riprovo tra 30s.', true);
  }
}

// ── Simple markdown renderer ────────────────────────────────────────────────
function renderMarkdown(text) {
  const el = document.createElement('div');
  // Code blocks
  let html = text.replace(/```([\s\S]*?)```/g, (_, code) =>
    `<pre><code>${code.replace(/</g,'&lt;').replace(/>/g,'&gt;')}</code></pre>`);
  // Inline code
  html = html.replace(/`([^`]+)`/g, (_, c) =>
    `<code>${c.replace(/</g,'&lt;').replace(/>/g,'&gt;')}</code>`);
  // Bold
  html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  // Italic
  html = html.replace(/\*([^*]+)\*/g, '<em>$1</em>');
  // Bullet lists
  html = html.replace(/^[-•] (.+)$/gm, '<li>$1</li>');
  html = html.replace(/(<li>.*<\/li>)/s, '<ul>$1</ul>');
  // Line breaks
  html = html.replace(/\n/g, '<br>');
  el.innerHTML = html;
  return el;
}

// ── Message rendering ────────────────────────────────────────────────────────
function appendMessage(role, text) {
  const chatWindow = document.getElementById('chatWindow');
  if (!chatWindow) {
    return;
  }
  const messageEl = document.createElement('div');
  messageEl.className = `message ${role}`;
  const bubble = document.createElement('div');
  bubble.className = `bubble ${role}`;
  if (role === 'assistant' && text) {
    bubble.appendChild(renderMarkdown(text));
  } else {
    bubble.textContent = text;
  }
  messageEl.appendChild(bubble);
  chatWindow.appendChild(messageEl);
  chatWindow.scrollTop = chatWindow.scrollHeight;
  bubble.scrollIntoView({ block: 'end' });
  return bubble;
}

// ── Response details (collapsible) ──────────────────────────────────────────
function appendDetails(bubble, data) {
  if (!bubble) return;
  const messageEl = bubble.parentElement;

  // Compact metrics line
  const latency = data.latency_ms;
  const kb = data.kb_used;
  if (latency !== undefined || kb) {
    const meta = document.createElement('div');
    meta.className = 'bubble-meta';
    const parts = [];
    if (kb) parts.push(`KB: ${kb}`);
    if (latency !== undefined) parts.push(`${latency}ms`);
    meta.textContent = parts.join(' · ');
    messageEl.appendChild(meta);
  }

  // Collapsible details
  const hasDoc = data.selected_doc && (data.selected_doc.title || data.selected_doc.text);
  const hasExtra = hasDoc || data.ollama_available !== undefined || data.session_id;
  if (!hasExtra) return;

  const details = document.createElement('details');
  details.className = 'response-details';

  const summary = document.createElement('summary');
  summary.textContent = 'Dettagli';
  details.appendChild(summary);

  const content = document.createElement('div');
  content.className = 'response-details-content';

  // Context document
  if (hasDoc) {
    const doc = data.selected_doc;
    const section = document.createElement('div');
    section.className = 'detail-section';
    const heading = document.createElement('div');
    heading.className = 'detail-heading';
    heading.textContent = 'Contesto knowledge base';
    section.appendChild(heading);
    if (doc.title) {
      const label = document.createElement('div');
      label.className = 'detail-label';
      label.textContent = doc.title;
      section.appendChild(label);
    }
    if (doc.text) {
      const text = document.createElement('div');
      text.className = 'detail-text';
      text.textContent = doc.text.length > 500 ? doc.text.slice(0, 500) + '\u2026' : doc.text;
      section.appendChild(text);
    }
    content.appendChild(section);
  }

  // Model status
  if (data.ollama_available !== undefined) {
    const row = document.createElement('div');
    row.className = 'detail-row';
    row.innerHTML = `<strong>Modello:</strong> ${data.ollama_available ? 'disponibile' : 'non disponibile'}`;
    content.appendChild(row);
    if (data.ollama_error) {
      const err = document.createElement('div');
      err.className = 'detail-row detail-error';
      err.textContent = `Errore: ${data.ollama_error}`;
      content.appendChild(err);
    }
  }

  // Session id
  if (data.session_id) {
    const row = document.createElement('div');
    row.className = 'detail-row';
    row.innerHTML = `<strong>Sessione:</strong> <code>${data.session_id}</code>`;
    content.appendChild(row);
  }

  details.appendChild(content);
  messageEl.appendChild(details);
}

// ── Retry-capable fetch  (4.4.8) ─────────────────────────────────────────────
async function fetchWithRetry(url, options, maxRetries = RETRY_MAX) {
  let lastError;
  for (let attempt = 1; attempt <= maxRetries; attempt++) {
    try {
      const res = await fetch(url, options);
      return res;
    } catch (err) {
      lastError = err;
      console.warn(`Fetch attempt ${attempt}/${maxRetries} failed:`, err);
      if (attempt < maxRetries) {
        await new Promise(resolve => setTimeout(resolve, RETRY_DELAY_MS * attempt));
      }
    }
  }
  throw lastError;
}

// ── Send question (streaming with fallback) ───────────────────────────────────
async function sendQuestion() {
  const questionField = document.getElementById('question');
  const button = document.getElementById('sendBtn');
  const question = questionField ? questionField.value.trim() : '';

  if (!question) {
    setStatus('Inserisci una domanda prima di inviare.', true);
    return;
  }

  if (button) button.disabled = true;

  appendMessage('user', question);
  if (questionField) questionField.value = '';

  setStatus('Invio in corso...', false);
  const loadingBubble = appendMessage('assistant', '');
  if (loadingBubble) loadingBubble.classList.add('streaming');

  const payload = { question };
  if (sessionId) payload.session_id = sessionId;

  try {
    const res = await fetchWithRetry('/proxy/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });

    if (!res.ok || !res.body) {
      throw new Error(`HTTP ${res.status}`);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let metaData = {};
    let gotDone = false;

    setStatus('Elaborazione...', false);

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop();

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const jsonStr = line.slice(6).trim();
        if (!jsonStr) continue;
        let event;
        try { event = JSON.parse(jsonStr); } catch { continue; }

        if (event.type === 'meta') {
          metaData = event;
          if (event.session_id) {
            sessionId = event.session_id;
            localStorage.setItem('brainhome_session', sessionId);
          }
        } else if (event.type === 'token') {
          if (loadingBubble) {
            // streaming: accumulate raw text, re-render markdown on each chunk
            loadingBubble._rawText = (loadingBubble._rawText || '') + event.text;
            loadingBubble.textContent = ''; // clear placeholder
            loadingBubble.classList.add('streaming');
            loadingBubble.appendChild(renderMarkdown(loadingBubble._rawText));
            const cw = document.getElementById('chatWindow');
            if (cw) cw.scrollTop = cw.scrollHeight;
          }
        } else if (event.type === 'done') {
          gotDone = true;
          if (loadingBubble) {
            loadingBubble.classList.remove('streaming');
            loadingBubble.textContent = '';
            const answer = (event.answer || '').trim();
            loadingBubble.appendChild(renderMarkdown(answer));
            // Tool badge
            const toolUsed = event.tool_used || (event.tools_used && event.tools_used.join(' → '));
            if (toolUsed) {
              const badge = document.createElement('span');
              badge.className = 'tool-badge';
              badge.textContent = '⚡ ' + toolUsed;
              loadingBubble.prepend(badge);
            }
            appendDetails(loadingBubble, { ...metaData, ...event });
          }
          setStatus('Risposta ricevuta.', false);
        }
      }
    }

    if (!gotDone && loadingBubble) {
      loadingBubble.classList.remove('streaming');
      if (!loadingBubble.textContent.trim()) {
        loadingBubble.textContent = 'Nessuna risposta ricevuta.';
      }
      setStatus('Stream terminato.', false);
    }
  } catch (err) {
    console.error('Stream error:', err);
    if (loadingBubble) {
      loadingBubble.classList.remove('streaming');
      loadingBubble.textContent = 'Impossibile contattare il server dopo ' + RETRY_MAX + ' tentativi. Controlla la connessione.';
    }
    setStatus('Errore di comunicazione.', true);
  } finally {
    if (button) button.disabled = false;
  }
}

// ── Init ──────────────────────────────────────────────────────────────────────
window.addEventListener('DOMContentLoaded', () => {
  const button = document.getElementById('sendBtn');
  const questionField = document.getElementById('question');

  if (button) {
    button.addEventListener('click', sendQuestion);
  } else {
    console.error('Send button non trovato');
    setStatus('Errore interno: bottone non trovato.', true);
  }

  if (questionField) {
    questionField.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        sendQuestion();
      }
    });
  }

  checkBackendHealth();
  // Auto-reconnect health check every 30 seconds  (4.4.8)
  setInterval(checkBackendHealth, HEALTH_INTERVAL_MS);
});


window.addEventListener('error', function (event) {
  setStatus('Errore JS: ' + event.message, true);
  console.error('Window error:', event);
});
