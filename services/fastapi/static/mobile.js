// ── Constants ──────────────────────────────────────────────────────────────
const RETRY_MAX = 3;
const RETRY_DELAY_MS = 1500;
const HEALTH_INTERVAL_MS = 30000;

// Per-agent session IDs (multi-turn, persisted in localStorage)
function getSession(agentId) {
  return localStorage.getItem(`brainhome_session_${agentId}`) || null;
}
function setSession(agentId, sid) {
  localStorage.setItem(`brainhome_session_${agentId}`, sid);
}

// Agent registry and selector state (populated by loadAgents())
let agents = [];
let selectedAgentId = 'all';

// Guard: prevents health-check from overwriting status while a query is running
let queryInFlight = false;

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

// ── Agent registry & selector ──────────────────────────────────────────────
const AGENT_COLORS = ['#0f9d58', '#1976D2', '#7B1FA2', '#E64A19', '#F57C00'];

async function loadAgents() {
  try {
    const res = await fetch('/agents');
    if (res.ok) agents = await res.json();
  } catch (e) {
    console.warn('Impossibile caricare agenti:', e);
  }
  if (!agents.length) {
    agents = [{ id: 'dify', name: 'Principale', mention: 'principale' }];
  }
  renderAgentSelector();
  updatePlaceholder();
}

function agentColor(agentId) {
  const idx = agents.findIndex(a => a.id === agentId);
  return AGENT_COLORS[Math.max(idx, 0) % AGENT_COLORS.length];
}

function agentDisplayName(agentId) {
  const agent = agents.find(a => a.id === agentId);
  return agent ? agent.name : agentId;
}

function renderAgentSelector() {
  const el = document.getElementById('agentSelector');
  if (!el) return;
  el.innerHTML = '';
  const allBtn = document.createElement('button');
  allBtn.className = 'agent-pill' + (selectedAgentId === 'all' ? ' active' : '');
  allBtn.setAttribute('aria-pressed', String(selectedAgentId === 'all'));
  allBtn.textContent = 'Tutti';
  allBtn.addEventListener('click', () => selectAgent('all'));
  el.appendChild(allBtn);
  agents.forEach((a, i) => {
    const btn = document.createElement('button');
    btn.className = 'agent-pill' + (selectedAgentId === a.id ? ' active' : '');
    btn.style.setProperty('--agent-color', AGENT_COLORS[i % AGENT_COLORS.length]);
    btn.setAttribute('aria-pressed', String(selectedAgentId === a.id));
    btn.textContent = a.name;
    btn.addEventListener('click', () => selectAgent(a.id));
    el.appendChild(btn);
  });
}

function selectAgent(agentId) {
  selectedAgentId = agentId;
  renderAgentSelector();
  updatePlaceholder();
}

function updatePlaceholder() {
  const q = document.getElementById('question');
  if (!q) return;
  if (selectedAgentId === 'all') {
    q.placeholder = 'Scrivi a tutti... oppure @nome per un agente specifico';
  } else {
    const a = agents.find(ag => ag.id === selectedAgentId);
    q.placeholder = `Scrivi a ${a ? a.name : selectedAgentId}...`;
  }
}

// Returns the list of target agents, respecting @mention override
function findAgentByToken(token) {
  const normalized = token.toLowerCase();
  return agents.find(a =>
    (a.mention || '').toLowerCase() === normalized ||
    a.name.toLowerCase() === normalized ||
    a.id.toLowerCase() === normalized
  );
}

function resolveTargets(text) {
  const m = text.match(/^@(\S+)/);
  if (m) {
    const mention = m[1].toLowerCase();
    const found = agents.find(a =>
      (a.mention || '').toLowerCase() === mention ||
      a.name.toLowerCase() === mention ||
      a.id.toLowerCase() === mention
    );
    if (found) return [found];
  }
  if (selectedAgentId === 'all') return agents.length ? agents : [{ id: 'dify', name: 'Principale', mention: 'principale' }];
  const single = agents.find(a => a.id === selectedAgentId);
  return single ? [single] : agents;
}

// ── Health check with auto-reconnect  (4.4.8) ──────────────────────────────
async function checkBackendHealth() {
  if (queryInFlight) return;  // don't interfere with an active query
  setStatus('Verifica backend...', false);
  try {
    const res = await fetch('/health');
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}`);
    }
    const data = await res.json();
    if (queryInFlight) return;  // query started while we were waiting
    if (data.status === 'ok') {
      setBackendStatus('Connesso', true);
      setStatus('Pronto.', false);
    } else {
      setBackendStatus('Non disponibile', false);
      setStatus('Backend non disponibile.', true);
    }
  } catch (err) {
    if (queryInFlight) return;
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
function appendMessage(role, text, agentName = null, agentColor = null) {
  const chatWindow = document.getElementById('chatWindow');
  if (!chatWindow) return;
  const messageEl = document.createElement('div');
  messageEl.className = `message ${role}`;
  if (role === 'assistant' && agentName) {
    const label = document.createElement('div');
    label.className = 'agent-label';
    label.style.color = agentColor || '#0f9d58';
    label.textContent = agentName;
    messageEl.appendChild(label);
  }
  const bubble = document.createElement('div');
  bubble.className = `bubble ${role}`;
  if (role === 'assistant' && agentColor) {
    bubble.style.borderLeftColor = agentColor;
  }
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
  const delegationCount = Array.isArray(data.delegations_used) ? data.delegations_used.length : 0;
  if (latency !== undefined || kb || delegationCount > 0) {
    const meta = document.createElement('div');
    meta.className = 'bubble-meta';
    const parts = [];
    if (kb) parts.push(`KB: ${kb}`);
    if (latency !== undefined) parts.push(`${latency}ms`);
    if (delegationCount > 0) parts.push(`Delega: ${delegationCount}`);
    meta.textContent = parts.join(' · ');
    messageEl.appendChild(meta);
  }

  // Collapsible details
  const hasDoc = data.selected_doc && (data.selected_doc.title || data.selected_doc.text);
  const hasDelegations = Array.isArray(data.delegations_used) && data.delegations_used.length > 0;
  const hasExtra = hasDoc || data.ollama_available !== undefined || data.session_id || hasDelegations;
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

  if (hasDelegations) {
    const section = document.createElement('div');
    section.className = 'detail-section';
    const heading = document.createElement('div');
    heading.className = 'detail-heading';
    heading.textContent = 'Deleghe coinvolte';
    section.appendChild(heading);

    data.delegations_used.forEach(item => {
      const row = document.createElement('div');
      row.className = 'detail-row';
      const targetName = item.target_agent_name || agentDisplayName(item.target_agent_id);
      const state = item.success ? 'ok' : 'errore';
      row.innerHTML = `<strong>${targetName}</strong> · ${item.mode || 'ask'} · ${state}`;
      section.appendChild(row);
      if (!item.success && item.error_message) {
        const err = document.createElement('div');
        err.className = 'detail-row detail-error';
        err.textContent = item.error_code ? `${item.error_code}: ${item.error_message}` : item.error_message;
        section.appendChild(err);
      }
    });
    content.appendChild(section);
  }

  details.appendChild(content);
  messageEl.appendChild(details);
}

function appendDelegationBadges(bubble, delegationsUsed) {
  if (!bubble || !Array.isArray(delegationsUsed) || delegationsUsed.length === 0) return;
  const fragment = document.createDocumentFragment();
  delegationsUsed.forEach(item => {
    const badge = document.createElement('span');
    const targetName = item.target_agent_name || agentDisplayName(item.target_agent_id);
    badge.className = item.success ? 'delegation-badge' : 'delegation-badge delegation-badge-error';
    badge.textContent = item.success
      ? `↗ ${targetName}`
      : `↗ ${targetName} · errore`;
    fragment.appendChild(badge);
  });
  bubble.prepend(fragment);
}

// ── Retry-capable fetch  (4.4.8) ─────────────────────────────────────────────
async function fetchWithRetry(url, options, maxRetries = RETRY_MAX) {
  let lastError;
  for (let attempt = 1; attempt <= maxRetries; attempt++) {
    try {
      const res = await fetch(url, options);
      return res;
    } catch (err) {
      if (err.name === 'AbortError' || err.message?.includes('aborted')) {
        throw err;
      }
      lastError = err;
      console.warn(`Fetch attempt ${attempt}/${maxRetries} failed:`, err);
      if (attempt < maxRetries) {
        await new Promise(resolve => setTimeout(resolve, RETRY_DELAY_MS * attempt));
      }
    }
  }
  throw lastError;
}

// ── Per-agent streaming ────────────────────────────────────────────────────────
async function streamAgent(agent, question, loadingBubble) {
  if (loadingBubble) {
    loadingBubble.classList.add('streaming');
    const ti = document.createElement('div');
    ti.className = 'typing-indicator';
    ti.innerHTML = '<span></span><span></span><span></span>';
    loadingBubble.appendChild(ti);
  }

  const payload = { question, target: agent.id };
  const sid = getSession(agent.id);
  if (sid) payload.session_id = sid;

  try {
    const res = await fetchWithRetry('/proxy/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });

    if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let metaData = {};
    let gotDone = false;

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
          if (event.session_id) setSession(agent.id, event.session_id);
        } else if (event.type === 'thinking') {
          if (loadingBubble && !loadingBubble._hasContent) {
            if (!loadingBubble.querySelector('.typing-indicator')) {
              loadingBubble.textContent = '';
              loadingBubble.classList.add('streaming');
              const ti = document.createElement('div');
              ti.className = 'typing-indicator';
              ti.innerHTML = '<span></span><span></span><span></span>';
              loadingBubble.appendChild(ti);
            }
          }
        } else if (event.type === 'tool_start') {
          if (loadingBubble) {
            const ti = loadingBubble.querySelector('.typing-indicator');
            if (ti) ti.remove();
            loadingBubble._hasContent = true;
            loadingBubble._toolActivity = loadingBubble._toolActivity || [];
            const indicator = document.createElement('div');
            indicator.className = 'tool-activity';
            indicator.dataset.tool = event.tool;
            indicator.innerHTML = `<span class="tool-spinner"></span><span class="tool-name">&#9881;&#65039; ${event.tool}</span>`;
            loadingBubble._toolActivity.push(indicator);
            loadingBubble.textContent = '';
            loadingBubble.classList.add('streaming');
            loadingBubble._toolActivity.forEach(el => loadingBubble.appendChild(el));
            const cw = document.getElementById('chatWindow');
            if (cw) cw.scrollTop = cw.scrollHeight;
          }
        } else if (event.type === 'tool_result') {
          if (loadingBubble && loadingBubble._toolActivity) {
            const indicator = loadingBubble._toolActivity.find(el => el.dataset.tool === event.tool);
            if (indicator) {
              indicator.classList.add('tool-done');
              indicator.querySelector('.tool-spinner').textContent = '\u2713';
            }
          }
        } else if (event.type === 'token') {
          if (loadingBubble) {
            if (loadingBubble._toolActivity && loadingBubble._toolActivity.length > 0) {
              loadingBubble.textContent = '';
              loadingBubble._toolActivity = [];
            }
            const ti = loadingBubble.querySelector('.typing-indicator');
            if (ti) ti.remove();
            loadingBubble._rawText = (loadingBubble._rawText || '') + event.text;
            loadingBubble.textContent = '';
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
            if (answer) {
              loadingBubble.appendChild(renderMarkdown(answer));
            } else {
              loadingBubble.textContent = '(nessuna risposta)';
            }
            const toolsUsed = event.tools_used && event.tools_used.length > 0
              ? event.tools_used
              : event.tool_used ? [event.tool_used] : [];
            if (toolsUsed.length > 0) {
              const badge = document.createElement('span');
              badge.className = 'tool-badge';
              badge.textContent = '\u26A1 ' + toolsUsed.join(' \u2192 ');
              loadingBubble.prepend(badge);
            }
            appendDelegationBadges(loadingBubble, event.delegations_used);
            appendDetails(loadingBubble, { ...metaData, ...event });
          }
        }
      }
    }

    if (!gotDone && loadingBubble) {
      loadingBubble.classList.remove('streaming');
      loadingBubble.textContent = 'Risposta non ricevuta. Riprova.';
    }
  } catch (err) {
    console.error(`Errore stream [${agent.id}]:`, err);
    if (loadingBubble) {
      loadingBubble.classList.remove('streaming');
      loadingBubble.textContent = 'Impossibile contattare il server dopo ' + RETRY_MAX + ' tentativi. Controlla la connessione.';
    }
  }
}

// ── Send question (multi-agent) ────────────────────────────────────────────────
async function sendQuestion() {
  const questionField = document.getElementById('question');
  const button = document.getElementById('sendBtn');
  const question = questionField ? questionField.value.trim() : '';

  if (!question) {
    setStatus('Inserisci una domanda prima di inviare.', true);
    return;
  }

  // Determine target agents — @mention in text overrides selector
  const targets = resolveTargets(question);
  if (!targets.length) {
    setStatus('Nessun agente disponibile.', true);
    return;
  }

  if (button) button.disabled = true;
  queryInFlight = true;

  const statusInterval = setInterval(() => {
    if (queryInFlight) setStatus('Elaborazione in corso...', false);
  }, 2000);

  appendMessage('user', question);
  if (questionField) questionField.value = '';
  setStatus('Invio in corso...', false);

  // Create one labeled loading bubble per target agent
  const bubbles = targets.map(agent => {
    const color = agentColor(agent.id);
    const showLabel = agents.length > 1;
    return appendMessage('assistant', '', showLabel ? agent.name : null, color);
  });

  try {
    // Stream all target agents in parallel
    await Promise.all(targets.map((agent, i) => streamAgent(agent, question, bubbles[i])));
    setStatus('Risposta ricevuta.', false);
  } finally {
    queryInFlight = false;
    clearInterval(statusInterval);
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

  loadAgents();
  checkBackendHealth();
  // Auto-reconnect health check every 30 seconds  (4.4.8)
  setInterval(checkBackendHealth, HEALTH_INTERVAL_MS);
});

window.addEventListener('error', function (event) {
  setStatus('Errore JS: ' + event.message, true);
  console.error('Window error:', event);
});
