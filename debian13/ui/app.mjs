import { readWorkspaceSources, readWithTimeout } from './global_sync.mjs';

export const AGENT_PROFILES = Object.freeze([
  { id: 'aria', name: 'Aria' },
  { id: 'astyria', name: 'Astyria' },
  { id: 'aurelius', name: 'Aurelius' },
  { id: 'alex', name: 'Alex' },
]);

const SESSION_KEY = 'debian13-chat-session';
const HISTORY_KEY = 'debian13-chat-history';
const SESSION_ID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

export class ApiError extends Error {
  constructor(message, status = 0) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

function apiErrorMessage(payload, status) {
  if (status === 401) return 'Brak autoryzacji API (401). Skonfiguruj DEBIAN13_API_TOKEN wyłącznie po stronie serwera proxy.';
  if (status === 403) return 'API odrzuciło uprawnienia (403). Sprawdź dostęp operatora.';
  if (status === 404) return 'Backend nie udostępnia tego endpointu (404).';
  return typeof payload?.error === 'string'
    ? payload.error
    : `Żądanie nie powiodło się (HTTP ${status}).`;
}

export function createApiClient({ fetchImpl = globalThis.fetch, baseUrl = '' } = {}) {
  async function fetchRequest(path, options) {
    try {
      return await fetchImpl(`${baseUrl}/api/v1/${path}`, options);
    } catch (error) {
      if (error?.name === 'AbortError') throw error;
      throw new ApiError('Runtime nieosiągalny — sprawdź połączenie z API.');
    }
  }

  async function request(path, options) {
    const response = await fetchRequest(path, options);
    let payload;
    try {
      payload = await response.json();
    } catch {
      throw new ApiError('Runtime zwrócił nieprawidłową odpowiedź.', response.status);
    }
    if (!response.ok) {
      throw new ApiError(apiErrorMessage(payload, response.status), response.status);
    }
    return payload;
  }

  function post(path, body) {
    return request(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  }

  async function chatStream(message, agent, sessionId, onEvent, signal) {
    const body = { message, agent };
    if (sessionId) body.session_id = sessionId;
    const response = await fetchRequest('chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal,
    });

    if (!response.ok) {
      let payload;
      try {
        payload = await response.json();
      } catch {
        throw new ApiError('Runtime zwrócił nieprawidłową odpowiedź.', response.status);
      }
      throw new ApiError(apiErrorMessage(payload, response.status), response.status);
    }
    if (!response.headers.get('content-type')?.toLowerCase().includes('text/event-stream') || !response.body) {
      throw new ApiError('Runtime nie zwrócił strumienia odpowiedzi.', response.status);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    const dispatch = (block) => {
      let event = 'message';
      const data = [];
      for (const line of block.split(/\r?\n/)) {
        if (line.startsWith('event:')) event = line.slice(6).trim();
        if (line.startsWith('data:')) data.push(line.slice(5).trimStart());
      }
      if (!data.length) return;
      let payload;
      try {
        payload = JSON.parse(data.join('\n'));
      } catch {
        throw new ApiError('Runtime wysłał nieprawidłowe zdarzenie czatu.');
      }
      onEvent({ event, payload });
    };

    while (true) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      let separator;
      while ((separator = /\r?\n\r?\n/.exec(buffer))) {
        dispatch(buffer.slice(0, separator.index));
        buffer = buffer.slice(separator.index + separator[0].length);
      }
      if (done) break;
    }
    if (buffer.trim()) dispatch(buffer);
  }

  return Object.freeze({
    getStatus: () => request('status'),
    getCodexStatus: () => request('codex/status'),
    getAiCenterStatus: () => request('ai-center/status'),
    getAgents: () => request('agents'),
    getOriginalAgents: () => request('original-agents'),
    getOriginalReviewCapabilities: () => request('original-agents/review-capabilities'),
    handoffOriginalAgent: (taskId) => request(`tasks/${encodeURIComponent(taskId)}/handoff`, {method: 'POST'}),
    getOriginalJob: (taskId) => request(`tasks/${encodeURIComponent(taskId)}/original-status`),
    getGitHubStatus: () => request('github/status'),
    getSyncState: () => request('sync/state'),
    getIntegrations: () => request('integrations'),
    getIntegrationProgress: () => request('integration-progress'),
    getTasks: () => request('tasks'),
    getAlexCapabilities: () => request('alex/capabilities'),
    dispatchToAlex: (taskId) => request(`tasks/${encodeURIComponent(taskId)}/dispatch`, {method: 'POST'}),
    createTask: (task) => post('tasks', task),
    getTask: (taskId) => request(`tasks/${encodeURIComponent(taskId)}`),
    getEvents: () => request('events'),
    getAgentDialog: (after = 0) => request(`agent-dialog/messages?after=${encodeURIComponent(after)}`),
    postAgentDialog: (to, content) => post('agent-dialog/messages', { to, content }),
    getMessages: (sessionId) => request(`sessions/${encodeURIComponent(sessionId)}/messages`),
    cancelTask: (taskId) => request(`tasks/${encodeURIComponent(taskId)}/cancel`, { method: 'POST' }),
    approveLocalAnalysis: (taskId) => request(`tasks/${encodeURIComponent(taskId)}/approve-analysis`, { method: 'POST' }),
    chatStream,
    vision: (agent, message, image, sessionId) => post('vision', { profile: agent, message, image, ...(sessionId ? { session_id: sessionId } : {}) }),
    terminal: (command) => post('terminal', { command }),
  });
}

export function getInitialAgentStatuses() {
  return AGENT_PROFILES.map((agent) => ({
    ...agent,
    status: 'unknown',
    worker: 'unknown',
  }));
}

function agentStatusLabel(agent) {
  const profile = agent.status === 'unassigned'
    ? 'Bez przydziału — oczekuje na polecenie właściciela'
    : agent.status === 'profile_configured'
      ? 'Profil dostępny — zadania nieprzydzielone'
      : 'Profil: stan nieznany';
  const worker = agent.worker === 'not_running'
    ? 'Worker nieuruchomiony'
    : agent.worker === 'running'
      ? 'Worker uruchomiony'
      : 'Worker: stan nieznany';
  return { profile, worker };
}

function renderAgentProfiles(list, agents = getInitialAgentStatuses()) {
  if (!list) return;
  const fragment = document.createDocumentFragment();
  for (const agent of agents) {
    const item = document.createElement('li');
    item.className = 'agent-card';
    item.dataset.agent = agent.id;

    const nameRow = document.createElement('div');
    nameRow.className = 'agent-name-row';
    const dot = document.createElement('span');
    dot.className = 'agent-state-dot';
    dot.setAttribute('aria-hidden', 'true');
    const name = document.createElement('span');
    name.textContent = agent.name;
    nameRow.append(dot, name);

    const role = document.createElement('p');
    role.className = 'agent-role';
    role.textContent = agent.role || 'Rola: nieprzydzielona';
    const labels = agentStatusLabel(agent);
    const profile = document.createElement('p');
    profile.className = 'agent-state';
    profile.textContent = labels.profile;
    const worker = document.createElement('p');
    worker.className = 'worker-state';
    worker.textContent = labels.worker;

    item.append(nameRow, role, profile, worker);
    fragment.append(item);
  }
  list.replaceChildren(fragment);
}

function setText(id, text) {
  const element = document.getElementById(id);
  if (element) element.textContent = text;
}

function connectionState(state, label) {
  const element = document.getElementById('connection-status');
  if (!element) return;
  element.dataset.state = state;
  const text = element.querySelector('span:last-child');
  if (text) text.textContent = label;
  setText('sidebar-runtime-label', label);
}

function modelStatusLabel(model) {
  const statusLabels = {
    not_configured: 'Brak skonfigurowanego modelu',
    unavailable: 'Model niedostępny',
    configured: 'Model skonfigurowany (połączenie jeszcze niezweryfikowane)',
    model_missing: 'Model niedostępny',
    model_available: 'Model dostępny lokalnie',
  };
  const label = statusLabels[model?.status] ?? 'Stan modelu nieznany';
  const inference = model?.inference === 'verified'
    ? 'inferencja zweryfikowana'
    : 'inferencja niezweryfikowana';
  return `${label} · ${inference}`;
}

function renderRuntimeStatus(status) {
  const application = status.application?.status === 'running'
    ? `${status.application.product ?? 'Aplikacja'} działa`
    : 'Aplikacja niedostępna';
  const osRuntime = status.operating_system?.runtime;
  const distribution = osRuntime?.distribution ?? 'dystrybucja nieznana';
  const boot = osRuntime?.debian_13_boot_verified === true
    ? 'boot Debian 13 potwierdzony'
    : 'boot Debian 13 niepotwierdzony';
  setText('application-status', application);
  setText('os-status', `${distribution} · ${boot}`);
  setText('model-status', modelStatusLabel(status.model));
  const svl = status.svl;
  setText('svl-status', svl?.stateValid === true
    ? `${svl.version ?? 'wersja nieznana'} · ${svl.storePresent ? 'stan z pliku' : 'ustawienia domyślne'} · ${svl.chatHistoryEntries ?? 0} wiadomości · bez potwierdzenia startu OS`
    : 'Silnik SVL niedostępny lub nieprawidłowy · OS nieuruchomiony');
  setText('task-storage-status', status.task_state ?? 'Stan pamięci zadań nieznany');
  setText('status-error', '');
  connectionState('connected', 'API dostępne');
}

function appendTranscript(kind, title, content = '') {
  const transcript = document.getElementById('chat-transcript');
  if (!transcript) return null;
  transcript.querySelector('.empty-state')?.remove();
  const entry = document.createElement('article');
  entry.className = `transcript-entry transcript-${kind}`;
  const heading = document.createElement('p');
  heading.className = 'transcript-heading';
  heading.textContent = title;
  const text = document.createElement('p');
  text.className = 'transcript-text';
  text.textContent = content;
  entry.append(heading, text);
  transcript.append(entry);
  transcript.scrollTop = transcript.scrollHeight;
  return { entry, heading, text };
}

function renderTranscriptMessages(messages) {
  const transcript = document.getElementById('chat-transcript');
  if (!transcript) return;
  transcript.replaceChildren();
  for (const message of messages) {
    if (!['user', 'assistant'].includes(message.role) || typeof message.content !== 'string') continue;
    const isUser = message.role === 'user';
    const name = AGENT_PROFILES.find((agent) => agent.id === message.agent)?.name ?? 'Agent';
    appendTranscript(
      isUser ? 'user' : 'assistant',
      isUser ? `Ty · ${name}` : `${name} · ${message.source ?? 'API'}`,
      message.content,
    );
  }
  if (!transcript.children.length) {
    const empty = document.createElement('p');
    empty.className = 'empty-state';
    empty.textContent = 'Ta sesja nie ma jeszcze zapisanych wiadomości.';
    transcript.append(empty);
  }
}

function renderTerminal(command, output, isError = false) {
  const terminal = document.getElementById('terminal-output');
  if (!terminal) return;
  terminal.querySelector('.terminal-hint')?.remove();
  const entry = document.createElement('p');
  const prompt = document.createElement('span');
  prompt.className = 'prompt-mark';
  prompt.setAttribute('aria-hidden', 'true');
  prompt.textContent = isError ? '!' : '$';
  const commandText = document.createElement('span');
  commandText.textContent = ` ${command}`;
  entry.append(prompt, commandText);
  const result = document.createElement('pre');
  result.className = isError ? 'terminal-error' : 'terminal-result';
  result.textContent = output;
  terminal.append(entry, result);
  terminal.scrollTop = terminal.scrollHeight;
}

function renderTasks(tasks) {
  const list = document.getElementById('task-list');
  if (!list) return;
  const fragment = document.createDocumentFragment();
  for (const task of tasks) {
    const item = document.createElement('li');
    item.className = 'task-item';
    item.dataset.taskId = task.id;
    const detail = document.createElement('span');
    detail.textContent = `${task.title || task.kind} · ${task.agent} · ${task.id}`;
    const status = document.createElement('span');
    status.className = `task-state task-${task.status}`;
    status.textContent = task.execution && task.execution !== 'not_started'
      ? task.execution.replaceAll('_', ' ') : task.status;
    const actions = document.createElement('span');
    actions.className = 'task-actions';
    if (typeof task.id === 'string' && SESSION_ID_PATTERN.test(task.id)) {
      const reconnect = document.createElement('button');
      reconnect.type = 'button';
      reconnect.className = 'task-action';
      reconnect.textContent = 'Pobierz status';
      reconnect.setAttribute('aria-label', `Pobierz status zadania ${task.id}`);
      reconnect.addEventListener('click', () => void refreshTaskStatus(task.id, reconnect));
      actions.append(reconnect);
      if (task.originalRemoteTaskId) {
        const inspect = document.createElement('button');
        inspect.type = 'button';
        inspect.className = 'task-action';
        inspect.textContent = 'Sprawdź oryginalne AI';
        inspect.addEventListener('click', () => void checkOriginalJob(task.id, inspect));
        actions.append(inspect);
      } else if (task.status === 'queued' && task.execution === 'not_started') {
        const handoff = document.createElement('button');
        handoff.type = 'button';
        handoff.className = 'task-action';
        handoff.textContent = 'Wyślij do ' + (AGENT_PROFILES.find(x => x.id === task.agent)?.name || 'AI');
        handoff.disabled = !originalReviewSupported;
        handoff.addEventListener('click', () => void sendToOriginalAgent(task.id, handoff));
        actions.append(handoff);
      }
      if (task.agent === 'alex' && task.status === 'queued' && !task.originalRemoteTaskId) {
        const dispatch = document.createElement('button');
        dispatch.type = 'button';
        dispatch.className = 'task-action task-dispatch';
        dispatch.textContent = 'Przekaż Alexowi';
        dispatch.disabled = !alexDispatchSupported;
        dispatch.setAttribute('aria-label', `Przekaż zadanie ${task.id} oryginalnemu Alexowi`);
        dispatch.addEventListener('click', () => void dispatchWorkToAlex(task.id, dispatch));
        actions.append(dispatch);
      }
      if (!task.originalRemoteTaskId && task.status === 'queued'
          && ['not_started', 'interrupted_local_analysis',
              'failed_local_analysis'].includes(task.execution)) {
        const approve = document.createElement('button');
        approve.type = 'button';
        approve.className = 'task-action';
        approve.textContent = 'Analizuj przez Ollamę';
        approve.setAttribute('aria-label', `Zatwierdź lokalną analizę zadania ${task.id}`);
        approve.addEventListener('click', () => void approveWorkForOllama(task.id, approve));
        actions.append(approve);
      }
      if (task.execution === 'completed_local_analysis') {
        const result = document.createElement('button');
        result.type = 'button';
        result.className = 'task-action';
        result.textContent = 'Pokaż raport';
        result.addEventListener('click', () => void showLocalAnalysis(task.id, result));
        actions.append(result);
      }
      if (!task.originalRemoteTaskId && ['queued', 'running'].includes(task.status)) {
        const cancel = document.createElement('button');
        cancel.type = 'button';
        cancel.className = 'task-action task-cancel';
        cancel.textContent = 'Anuluj';
        cancel.setAttribute('aria-label', `Anuluj zadanie ${task.id}`);
        cancel.addEventListener('click', () => void cancelWorkTask(task.id, cancel));
        actions.append(cancel);
      }
    }
    item.append(detail, status, actions);
    fragment.append(item);
  }
  if (!tasks.length) {
    const empty = document.createElement('li');
    empty.className = 'empty-state';
    empty.textContent = 'Brak zapisanych zadań do przeglądu.';
    fragment.append(empty);
  }
  list.replaceChildren(fragment);
  setText('tasks-count', `${tasks.length} zadań · trwały zapis SQLite; status workera w Centrum AI`);
  setText('tasks-error', '');
}

function renderEvents(events) {
  const list = document.getElementById('event-list');
  if (!list) return;
  const fragment = document.createDocumentFragment();
  for (const event of events) {
    if (
      typeof event?.type !== 'string'
      || typeof event.agent !== 'string'
      || typeof event.task_id !== 'string'
    ) continue;
    const item = document.createElement('li');
    item.className = 'event-item';
    const detail = document.createElement('span');
    const agent = AGENT_PROFILES.find((profile) => profile.id === event.agent)?.name ?? event.agent;
    detail.textContent = `${event.type} · ${agent}`;
    const metadata = document.createElement('span');
    metadata.className = 'event-metadata';
    const time = new Date(event.at);
    const timestamp = Number.isNaN(time.getTime()) ? 'czas nieznany' : time.toLocaleString('pl-PL');
    metadata.textContent = `${timestamp} · ${event.task_id}`;
    item.append(detail, metadata);
    fragment.append(item);
  }
  if (!fragment.childNodes.length) {
    const empty = document.createElement('li');
    empty.className = 'empty-state';
    empty.textContent = events.length
      ? 'Brak zdarzeń o rozpoznanym formacie.'
      : 'API nie zarejestrowało jeszcze zdarzeń.';
    fragment.append(empty);
  }
  list.replaceChildren(fragment);
  setText('events-count', `${events.length} zdarzeń · trwały dziennik SQLite`);
  setText('events-error', '');
}

function readHistory() {
  try {
    const history = JSON.parse(localStorage.getItem(HISTORY_KEY) ?? '[]');
    return Array.isArray(history)
      ? history.filter((item) => item && SESSION_ID_PATTERN.test(item.id) && typeof item.title === 'string').slice(0, 20)
      : [];
  } catch {
    return [];
  }
}

function writeHistory(history) {
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(history.slice(0, 20)));
  } catch {
    setText('chat-feedback', 'Indeks historii nie może zostać zapisany w przeglądarce.');
  }
}

function renderHistory(activeId = '') {
  const list = document.getElementById('chat-history');
  if (!list) return;
  const history = readHistory();
  list.replaceChildren();
  if (!history.length) {
    const empty = document.createElement('li');
    empty.className = 'history-empty';
    empty.textContent = 'Brak zapisanych rozmów.';
    list.append(empty);
    return;
  }
  for (const item of history) {
    const row = document.createElement('li');
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'history-button';
    button.textContent = item.title;
    button.dataset.sessionId = item.id;
    if (item.id === activeId) button.setAttribute('aria-current', 'true');
    button.addEventListener('click', () => void loadHistory(item.id));
    row.append(button);
    list.append(row);
  }
}

const api = createApiClient();
let syncedUiTitle = 'Dialog Triady';
let syncedRevision = -1;

function displaySyncState(data) {
  if (!Number.isSafeInteger(data?.revision) || data.revision < 0
      || typeof data.title !== 'string' || data.title.length > 80
      || !data.title.trim()) {
    throw new ApiError('Serwer synchronizacji zwrócił nieprawidłowe dane.');
  }
  if (data.revision < syncedRevision) return;
  syncedRevision = data.revision;
  syncedUiTitle = data.title;
  setText('sync-chat-title', data.title);
  const chatView = document.getElementById('chat-view');
  if (chatView && !chatView.hidden) setText('page-title', data.title);
  setText('sync-status', 'Synchronizacja SQLite · wersja ' + data.revision);
  const items = document.getElementById('sync-event-list');
  if (items) {
    const fragment = document.createDocumentFragment();
    for (const item of (Array.isArray(data.events) ? data.events : []).slice(0, 5)) {
      if (!Number.isSafeInteger(item.revision) || typeof item.label !== 'string') continue;
      const li = document.createElement('li');
      li.textContent = 'v' + item.revision + ' · ' + item.label;
      fragment.append(li);
    }
    items.replaceChildren(fragment);
  }
}

async function refreshSyncState() {
  try {
    const data = await api.getSyncState();
    displaySyncState(data);
    return data;
  } catch (error) {
    setText('sync-status', 'Synchronizacja niedostępna · ' + error.message);
    return null;
  }
}

function renderGithubItems(items, elementId, category) {
  const target = document.getElementById(elementId);
  if (!target) return;
  const fragment = document.createDocumentFragment();
  for (const item of Array.isArray(items) ? items : []) {
    if (typeof item.title !== 'string' || typeof item.url !== 'string') continue;
    const li = document.createElement('li');
    li.className = 'github-item';
    const a = document.createElement('a');
    a.href = item.url;
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    a.textContent = (category === 'runs' ? 'Run ' : '#') + item.number + ' · ' + item.title;
    const label = document.createElement('small');
    label.textContent = category === 'runs'
      ? (item.conclusion || item.status || 'Brak wyniku')
      : (item.draft ? 'Draft · ' : '') + (item.state || 'Nieznany');
    li.append(a, label);
    fragment.append(li);
  }
  if (!fragment.hasChildNodes()) {
    const empty = document.createElement('li');
    empty.textContent = 'Brak pozycji w zweryfikowanym odczycie.';
    fragment.append(empty);
  }
  target.replaceChildren(fragment);
}

async function refreshGithub() {
  setText('github-error', '');
  setText('github-access', 'Pobieranie z GitHub API…');
  const button = document.getElementById('github-refresh');
  if (button) button.disabled = true;
  try {
    const result = await api.getGitHubStatus();
    if (typeof result.repository !== 'string' || !Array.isArray(result.pullRequests)
        || !Array.isArray(result.issues) || !Array.isArray(result.runs)) {
      throw new ApiError('Nieoczekiwana odpowiedź GitHub.');
    }
    setText('github-repository', result.repository);
    setText('github-access', result.githubApiReachable
      ? (result.partial ? 'Połączony · częściowe dane' : 'Połączony · odczyt danych')
      : 'GitHub API niedostępne');
    const ts = new Date(result.observedAt);
    setText('github-observed', Number.isNaN(ts.getTime()) ? 'Nieznany' : ts.toLocaleString('pl-PL'));
    renderGithubItems(result.pullRequests, 'github-prs', 'prs');
    renderGithubItems(result.issues, 'github-issues', 'issues');
    renderGithubItems(result.runs, 'github-runs', 'runs');
    setText('github-prs-count', String(result.pullRequests.length));
    setText('github-issues-count', String(result.issues.length));
    setText('github-runs-count', String(result.runs.length));
    const errors = Object.entries(result.errors || {}).map(([k,v]) => k + ': ' + v);
    if (errors.length) setText('github-error', errors.join(' · '));
  } catch (error) {
    setText('github-access', 'Niepołączony');
    setText('github-error', error.message);
    renderGithubItems([], 'github-prs', 'prs');
    renderGithubItems([], 'github-issues', 'issues');
    renderGithubItems([], 'github-runs', 'runs');
  } finally {
    if (button) button.disabled = false;
  }
}

let alexDispatchSupported = false;
let originalReviewSupported = false;
let activeSessionId = '';
let activeChatController = null;
let activeChatTaskId = '';
let activeChatCancelMessage = '';

async function loadStatus() {
  connectionState('loading', 'Ładowanie statusu…');
  setText('status-error', '');
  for (const id of ['application-status', 'os-status', 'model-status', 'task-storage-status', 'svl-status']) {
    setText(id, 'Ładowanie…');
  }
  try {
    renderRuntimeStatus(await api.getStatus());
  } catch (error) {
    connectionState('error', error.status === 401 ? 'Wymagana autoryzacja' : 'Runtime niedostępny');
    for (const id of ['application-status', 'os-status', 'model-status', 'task-storage-status', 'svl-status']) {
      setText(id, 'Niedostępny');
    }
    setText('status-error', error.message);
  }
}

async function loadOriginalAgents() {
  const list = document.getElementById('original-agent-list');
  if (!list) return;
  setText('original-agent-error', '');
  try {
    const response = await api.getOriginalAgents();
    if (!Array.isArray(response.agents) || response.agents.length !== 4) {
      throw new ApiError('Źródło nie podało kompletu czterech oryginalnych agentów.');
    }
    const fragment = document.createDocumentFragment();
    for (const agent of response.agents) {
      const item = document.createElement('li');
      item.className = 'agent-card';
      const heading = document.createElement('strong');
      heading.textContent = agent.name;
      const role = document.createElement('p');
      role.className = 'agent-role';
      role.textContent = agent.role || 'Rola: nieprzydzielona';
      const status = document.createElement('p');
      status.className = 'agent-state';
      status.textContent = agent.workerVerified === true
        ? 'Zweryfikowany proces AI'
        : response.registryReachable
          ? 'Rejestr źródłowy dostępny · praca AI niepotwierdzona'
          : 'Oryginalny agent niepodłączony';
      const details = document.createElement('p');
      details.className = 'worker-state';
      details.textContent = `Zadania w rejestrze: ${Number(agent.tasksInRegistry) || 0}; uruchomione workery: niepotwierdzone`;
      item.append(heading, role, status, details);
      fragment.append(item);
    }
    list.replaceChildren(fragment);
  } catch (error) {
    list.replaceChildren();
    setText('original-agent-error', error.message);
  }
}

async function loadAgents() {
  setText('agent-count', 'Ładowanie profili…');
  setText('agent-error', '');
  renderAgentProfiles(document.getElementById('agent-list'));
  try {
    const payload = await api.getAgents();
    if (!Array.isArray(payload.agents)) throw new ApiError('Nieprawidłowy format listy profili.');
    const agents = payload.agents;
    renderAgentProfiles(document.getElementById('agent-list'), agents);
    const workers = agents.filter(({ worker }) => worker === 'running').length;
    setText('agent-count', `${agents.length} profili · ${workers} workerów uruchomionych`);
  } catch (error) {
    setText('agent-count', 'Profile niedostępne');
    setText('agent-error', error.message);
    renderAgentProfiles(document.getElementById('agent-list'));
  }
}

async function loadAiCenterStatus() {
  if (!document.getElementById('ai-center-worker-status')) return;
  setText('ai-center-error', '');
  try {
    const status = await api.getAiCenterStatus();
    if (status.type !== 'local_ollama_queue' || typeof status.workerHeartbeatVerified !== 'boolean') {
      throw new ApiError('Serwer zwrócił nieprawidłowy status Centrum AI.');
    }
    setText('ai-center-worker-badge', status.workerHeartbeatVerified ? 'Worker online' : 'Worker offline');
    setText('ai-center-worker-status', status.workerHeartbeatVerified
      ? 'Proces lokalny potwierdzony heartbeat; wnioskowanie wymaga testu'
      : 'Brak aktywnego heartbeat');
    setText('ai-center-worker-age', status.heartbeatAgeSeconds == null
      ? 'Brak' : `${status.heartbeatAgeSeconds} sekund temu`);
    setText('ai-center-model', status.modelConfigured || 'Brak konfiguracji');
    const jobs = status.queuedByExecution || {};
    setText('ai-center-queue', Object.entries(jobs).length
      ? Object.entries(jobs).map(([state, count]) => `${state.replaceAll('_',' ')}: ${count}`).join(' · ')
      : 'Brak przetworzonych lub zatwierdzonych zadań');
  } catch (error) {
    setText('ai-center-error', error.message);
    setText('ai-center-worker-badge', 'Brak połączenia');
    setText('ai-center-worker-status', 'Niezweryfikowany');
  }
}

async function loadCodexStatus() {
  if (!document.getElementById('codex-cli-status')) return;
  setText('codex-error', '');
  try {
    const status = await api.getCodexStatus();
    if (status.module !== 'codex' || typeof status.cliInstalled !== 'boolean') {
      throw new ApiError('Codex API zwróciło nieprawidłowy status.');
    }
    setText('codex-cli-status', status.cliInstalled ? 'Zainstalowany na hoście API' : 'Brak na hoście API');
    setText('codex-version-status', status.version ?? 'Niedostępna');
    const auth = {authenticated: 'Zalogowano na hoście API', not_authenticated: 'Wymagane logowanie',
                  not_installed: 'Najpierw zainstaluj Codex CLI', check_unavailable: 'Nie udało się sprawdzić',
                  not_checked: 'Nie sprawdzono', unknown: 'Stan niejednoznaczny'};
    setText('codex-login-status', auth[status.authentication] ?? 'Nie potwierdzono');
    setText('codex-worker-status', status.cloudWorkerVerified === true
      ? 'Zweryfikowany worker' : 'Niezweryfikowany / nieuruchomiony przez ten panel');
  } catch (error) {
    setText('codex-error', error.message);
    for (const id of ['codex-cli-status', 'codex-login-status',
                      'codex-version-status', 'codex-worker-status']) {
      setText(id, 'Nie udało się pobrać');
    }
  }
}

async function loadIntegrationProgress() {
  const list = document.getElementById('integration-progress-list');
  if (!list) return;
  list.replaceChildren();
  try {
    const data = await api.getIntegrationProgress();
    if (data.kind !== 'local_integration_checklist'
        || data.notOsCompletion !== true || data.notDeploymentProof !== true
        || !Number.isInteger(data.percentage)
        || !Array.isArray(data.modules)) {
      throw new ApiError('Nieprawidłowe dane postępu integracji.');
    }
    setText('integration-progress-total',
      `${data.percentage}% kontroli integracji (${data.verifiedCount}/${data.totalCount})`);
    for (const module of data.modules) {
      if (!Number.isInteger(module.percentage) || module.percentage < 0
          || module.percentage > 100 || !Array.isArray(module.checkpoints)) {
        throw new ApiError('Błędny wpis na liście integracji.');
      }
      const item = document.createElement('li');
      item.className = 'agent-card';
      const heading = document.createElement('strong');
      heading.textContent = `${module.name}: ${module.percentage}%`;
      const bar = document.createElement('progress');
      bar.value = module.percentage;
      bar.max = 100;
      bar.style.width = '100%';
      bar.setAttribute('aria-label', `Poziom kontroli: ${module.name}`);
      const details = document.createElement('p');
      details.className = 'agent-role';
      const passed = module.checkpoints.filter(check => check.verified === true).length;
      details.textContent = `${passed}/${module.checkpoints.length} kontroli potwierdzonych. `
        + (module.connected === true ? 'Ostatni etap potwierdzony.'
          : 'Brak potwierdzenia końcowego połączenia/wykonania.');
      item.append(heading, bar, details);
      list.append(item);
    }
    setText('integration-progress-error', '');
  } catch (error) {
    setText('integration-progress-total', 'Postęp niezweryfikowany');
    setText('integration-progress-error', error.message);
  }
}

async function loadIntegrations() {
  const list = document.getElementById('integration-list');
  if (!list) return;
  list.replaceChildren();
  try {
    const response = await api.getIntegrations();
    if (!Array.isArray(response.integrations)) throw new ApiError('Nieprawidłowa odpowiedź integracji.');
    const issueUrls = {
      mcp: 'https://github.com/mmitek515-netizen/Debian_13/issues/4',
      gpt_actions: 'https://github.com/mmitek515-netizen/Debian_13/issues/5',
      codex: 'https://github.com/mmitek515-netizen/Debian_13/issues/3',
    };
    for (const item of response.integrations) {
      const entry = document.createElement('li');
      entry.className = 'agent-card';
      const label = document.createElement('strong');
      label.textContent = item.name;
      const status = document.createElement('p');
      status.className = 'agent-state';
      status.textContent = item.connected === true
        ? 'Połączenie potwierdzone'
        : 'Niepołączono · ' + item.status;
      const description = document.createElement('p');
      description.className = 'agent-role';
      description.textContent = item.description;
      entry.append(label, status, description);
      if (issueUrls[item.id]) {
        const issue = document.createElement('a');
        issue.href = issueUrls[item.id];
        issue.target = '_blank';
        issue.rel = 'noopener noreferrer';
        issue.textContent = 'Zgłoszenie GitHub';
        entry.append(issue);
      }
      list.append(entry);
    }
    setText('integration-error', '');
  } catch (error) {
    setText('integration-error', error.message);
  }
}

async function loadTasks() {
  setText('tasks-count', 'Ładowanie…');
  setText('tasks-error', '');
  try {
    const payload = await api.getTasks();
    if (!Array.isArray(payload.tasks)) throw new ApiError('Nieprawidłowy format listy zadań.');
    renderTasks(payload.tasks);
    return null;
  } catch (error) {
    setText('tasks-count', 'Zadania niedostępne');
    setText('tasks-error', error.message);
    const list = document.getElementById('task-list');
    if (list) list.replaceChildren();
    return error;
  }
}

async function loadEvents() {
  setText('events-count', 'Pobieranie zdarzeń…');
  setText('events-error', '');
  const list = document.getElementById('event-list');
  if (list) {
    const loading = document.createElement('li');
    loading.className = 'empty-state';
    loading.textContent = 'Pobieranie zdarzeń z API…';
    list.replaceChildren(loading);
  }
  try {
    const payload = await api.getEvents();
    if (!Array.isArray(payload.events)) throw new ApiError('Nieprawidłowy format dziennika zdarzeń.');
    renderEvents(payload.events);
    return null;
  } catch (error) {
    setText('events-count', 'Zdarzenia niedostępne');
    setText('events-error', error.message);
    if (list) list.replaceChildren();
    return error;
  }
}

async function refreshAlexCapabilities() {
  alexDispatchSupported = false;
  try {
    const response = await api.getAlexCapabilities();
    const state = response.alex;
    alexDispatchSupported = state?.configured === true && state?.supported === true;
    setText('alex-dispatch-state', alexDispatchSupported
      ? 'Oryginalny Alex udostępnia protokół przekazania zadania. Nie potwierdzono jednak pracy modelu AI.'
      : 'Oryginalny Alex nie udostępnia obecnie bezpiecznego wysyłania zadań. Przycisk przekazania jest zablokowany.');
  } catch (error) {
    setText('alex-dispatch-state', 'Nie można sprawdzić połączenia z oryginalnym Alexem: ' + error.message);
  }
  return alexDispatchSupported;
}

async function refreshOriginalReviewCapabilities() {
  originalReviewSupported = false;
  try {
    const response = await api.getOriginalReviewCapabilities();
    const state = response.originalAgents;
    originalReviewSupported = state?.supported === true && state?.allowed === true;
    setText('original-review-state', originalReviewSupported
      ? 'API oryginalnych AI potwierdziło zgodny protokół kolejki Ollama. To nie dowodzi wykonania zadania.'
      : 'Wysyłanie do oryginalnych AI nieaktywne. Wymaga włączenia opt-in i zgodnego źródła.');
  } catch (error) {
    setText('original-review-state', 'Oryginalne AI niedostępne: ' + error.message);
  }
  return originalReviewSupported;
}

async function sendToOriginalAgent(taskId, button) {
  if (!originalReviewSupported || button.disabled) return;
  if (!window.confirm(
    'Przekazać zadanie do oryginalnego AI Studio? To zlecenie analizy przez Ollamę, nie zapis kodu, nie zakup i nie automatyczny PR.'
  )) return;
  button.disabled = true;
  try {
    const data = await api.handoffOriginalAgent(taskId);
    if (!data.originalSubmission || data.executionStarted !== false) {
      throw new ApiError('Źródło nie potwierdziło przyjęcia zadania.');
    }
    setText('work-connection-state', 'Źródło AI przyjęło zadanie. Wynik sprawdzisz przyciskiem „Sprawdź oryginalne AI”.');
    await refreshWork();
  } catch (error) {
    setText('tasks-error', error.message);
  } finally {
    button.disabled = false;
  }
}

async function checkOriginalJob(taskId, button) {
  button.disabled = true;
  try {
    const response = await api.getOriginalJob(taskId);
    const result = response.originalJob;
    if (!result || typeof result.status !== 'string') {
      throw new ApiError('Brak zweryfikowanego statusu oryginalnego AI.');
    }
    const item = button.closest('[data-task-id]');
    const state = item?.querySelector('.task-state');
    if (state) state.textContent = 'Oryginalne AI: ' + result.status
      + (result.agentExecutionVerified ? ' · model odpowiedział' : ' · bez dowodu ukończenia');
    const previous = item?.querySelector('.original-ai-result');
    previous?.remove();
    if (result.agentExecutionVerified && typeof result.result === 'string') {
      const report = document.createElement('pre');
      report.className = 'original-ai-result';
      report.textContent = result.result;
      item?.append(report);
    }
    setText('work-connection-state',
      result.agentExecutionVerified
        ? 'Zweryfikowano zapis odpowiedzi modelu. To nadal NIE potwierdza zmian w GitHub.'
        : 'Zdalny status: ' + result.status + '. Nie potwierdzono wykonania przez model.');
  } catch (error) {
    setText('tasks-error', error.message);
  } finally {
    button.disabled = false;
  }
}

async function dispatchWorkToAlex(taskId, button) {
  if (!alexDispatchSupported || button.disabled) return;
  const approved = window.confirm(
    'Czy przekazać to zadanie do oryginalnego Alexa? Nie oznacza to zgody na zakup, scalanie ani wykonywanie płatności.'
  );
  if (!approved) return;
  button.disabled = true;
  setText('work-connection-state', 'Weryfikacja odbioru zadania przez serwer Alexa…');
  try {
    const result = await api.dispatchToAlex(taskId);
    if (!result.alexSubmission || result.executionStarted !== false) {
      throw new ApiError('Brak potwierdzenia przyjęcia zadania przez oryginalne API Alexa.');
    }
    setText('work-connection-state', 'Oryginalny serwer Alexa przyjął zadanie. Wykonanie przez AI i PR pozostają niepotwierdzone.');
    await refreshWork();
  } catch (error) {
    setText('tasks-error', error.message);
    setText('work-connection-state', error.message);
  } finally {
    button.disabled = false;
  }
}

async function refreshWork() {
  const button = document.getElementById('refresh-tasks');
  if (button?.disabled) return;
  if (button) {
    button.disabled = true;
    button.textContent = 'Łączenie…';
  }
  setText('work-connection-state', 'Ponowne łączenie z API…');
  const [tasksError, eventsError] = await Promise.all([
    loadTasks(), loadEvents(), refreshAlexCapabilities(), refreshOriginalReviewCapabilities(),
  ]);
  // The buttons were initially rendered before reading actual remote capabilities.
  if (!tasksError) await loadTasks();
  const error = tasksError ?? eventsError;
  setText('work-connection-state', error
    ? error.status === 401
      ? 'Brak autoryzacji — skonfiguruj DEBIAN13_API_TOKEN po stronie serwera proxy.'
      : error.status === 403
        ? 'Dostęp do Work został odrzucony przez API.'
        : error.status === 404
          ? 'Ten backend nie udostępnia części funkcji Work.'
          : 'Połączenie z Work niedostępne. Użyj „Ponów połączenie”.'
    : 'Połączono z API. Zadania i zdarzenia odświeżone.');
  if (button) {
    button.textContent = error ? 'Ponów połączenie' : 'Odśwież';
    button.disabled = false;
  }
  return !error;
}

async function refreshTaskStatus(taskId, button) {
  button.disabled = true;
  try {
    const payload = await api.getTask(taskId);
    if (!payload.task || payload.task.id !== taskId || typeof payload.task.status !== 'string') {
      throw new ApiError('API zwróciło nieprawidłowy status zadania.');
    }
    const item = button.closest('[data-task-id]');
    const status = item?.querySelector('.task-state');
    if (status) {
      status.textContent = payload.task.status;
      status.className = `task-state task-${payload.task.status}`;
    }
    setText('work-connection-state', `Status zadania pobrany z API: ${payload.task.status}.`);
    setText('tasks-error', '');
  } catch (error) {
    setText('tasks-error', error.message);
    setText('work-connection-state', error.status === 404
      ? 'Szczegółowy status zadania nie jest dostępny w tym backendzie.'
      : error.message);
  } finally {
    button.disabled = false;
  }
}

async function approveWorkForOllama(taskId, button) {
  if (button.disabled) return;
  if (!window.confirm('Zatwierdzić lokalną analizę tego zadania przez Ollamę? Worker zapisze raport. Nie uruchomi poleceń ani nie zmieni repozytorium.')) return;
  button.disabled = true;
  try {
    const response = await api.approveLocalAnalysis(taskId);
    if (response.task?.execution !== 'approved_local_analysis') {
      throw new ApiError('Serwer nie potwierdził akceptacji zadania.');
    }
    setText('work-connection-state', response.localWorkerHeartbeatVerified
      ? 'Zatwierdzono zadanie. Aktywny worker Ollamy pobierze je z kolejki.'
      : 'Zatwierdzono zadanie. Oczekuje na uruchomienie workera Ollamy.');
    await refreshWork();
  } catch (error) {
    setText('tasks-error', error.message);
    button.disabled = false;
  }
}

async function showLocalAnalysis(taskId, button) {
  button.disabled = true;
  try {
    const payload = await api.getTask(taskId);
    const report = payload.localAnalysis;
    if (!report || typeof report.content !== 'string') {
      throw new ApiError('Raport nie jest jeszcze dostępny.');
    }
    const item = button.closest('[data-task-id]');
    if (!item) return;
    let detail = item.querySelector('.ai-task-result');
    if (!detail) {
      detail = document.createElement('pre');
      detail.className = 'ai-task-result';
      item.append(detail);
    }
    detail.textContent = report.content;
    setText('work-connection-state', `Raport modelu ${report.model} (propozycja; żadnych zmian w plikach).`);
  } catch (error) {
    setText('tasks-error', error.message);
  } finally {
    button.disabled = false;
  }
}

async function cancelWorkTask(taskId, button) {
  button.disabled = true;
  setText('work-connection-state', 'Wysyłanie żądania anulowania do API…');
  try {
    const payload = await api.cancelTask(taskId);
    if (!payload.task || payload.task.id !== taskId) {
      throw new ApiError('API nie potwierdziło zadania do anulowania.');
    }
    await refreshWork();
    setText('work-connection-state', `API przyjęło żądanie anulowania: ${payload.task.status}.`);
  } catch (error) {
    setText('tasks-error', error.message);
    setText('work-connection-state', error.status === 404
      ? 'Ten backend nie obsługuje anulowania zadań.'
      : error.message);
    button.disabled = false;
  }
}

async function refreshDashboard() {
  await Promise.all([loadStatus(), loadAgents(), loadOriginalAgents(), loadIntegrations(), loadIntegrationProgress(), loadTasks(), loadAiCenterStatus()]);
}

let globalSyncBusy = false;

async function synchronizeAllData() {
  if (globalSyncBusy) return;
  const button = document.getElementById('global-sync');
  const status = document.getElementById('global-sync-status');
  if (!button || !status) return;
  globalSyncBusy = true;
  button.disabled = true;
  button.textContent = 'Synchronizacja…';
  status.textContent = 'Sprawdzanie źródeł danych API…';
  try {
    // This checks current data only; GitHub checkout/deploy and external AI work
    // require separate authorized operations. Never claim a code deployment.
    const evidence = await readWorkspaceSources(api);
    // Bound the second-stage UI rendering too: source probes alone are not
    // enough when a dashboard request becomes unresponsive.
    const refreshed = await Promise.allSettled([
      () => refreshSyncState(), () => refreshDashboard(), () => refreshWork(),
      () => refreshGithub(), () => loadCodexStatus(),
    ].map(view => readWithTimeout(view)));
    const syncState = refreshed[0];
    const workState = refreshed[2];
    const renderFailed = refreshed.some(x => x.status === 'rejected')
      || (syncState.status === 'fulfilled' && syncState.value === null)
      || (workState.status === 'fulfilled' && workState.value === false);
    let historyFailed = false;
    if (activeSessionId && !activeChatController) {
      try {
        await readWithTimeout(() => loadHistory(activeSessionId));
      } catch {
        historyFailed = true;
      }
    }
    document.getElementById('agent-dialog-refresh')?.click();
    const missing = evidence.failed.join(', ');
    const partial = !evidence.complete || renderFailed || historyFailed;
    status.textContent = partial
      ? `Odczyt ${evidence.successful}/${evidence.total} źródeł`
          + (missing ? ` · niedostępne: ${missing}` : ' · część widoków niedostępna')
          + '. Bez wdrażania kodu GitHub.'
      : `Odczyt ${evidence.successful}/${evidence.total} źródeł potwierdzony. `
          + 'Odświeżono panel; kod GitHub nie był wdrażany.';
  } catch {
    status.textContent = 'Nie udało się odczytać stanu. Nic nie wdrożono ani nie zmieniono na GitHubie.';
  } finally {
    globalSyncBusy = false;
    button.disabled = false;
    button.textContent = 'Synchronizuj dane';
  }
}


async function loadHistory(sessionId) {
  if (!SESSION_ID_PATTERN.test(sessionId)) return;
  setText('chat-feedback', 'Pobieranie historii rozmowy…');
  try {
    const payload = await api.getMessages(sessionId);
    if (!Array.isArray(payload.messages)) throw new ApiError('Nieprawidłowy format historii rozmowy.');
    activeSessionId = sessionId;
    try {
      sessionStorage.setItem(SESSION_KEY, sessionId);
    } catch {
      setText('chat-feedback', 'Historia dostępna do końca tej karty przeglądarki.');
    }
    renderTranscriptMessages(payload.messages);
    renderHistory(activeSessionId);
    showView('chat');
    setText('chat-feedback', 'Historia Dialogu Triady odtworzona z lokalnej bazy SQLite.');
  } catch (error) {
    const history = readHistory().filter((item) => item.id !== sessionId);
    writeHistory(history);
    renderHistory(activeSessionId);
    setText('chat-feedback', error.status === 404
      ? 'Ta sesja wygasła lub backend został uruchomiony ponownie.'
      : error.message);
  }
}

function recordHistory(sessionId, title) {
  const history = readHistory().filter((item) => item.id !== sessionId);
  history.unshift({ id: sessionId, title: title.slice(0, 64) || 'Nowa rozmowa' });
  writeHistory(history);
  renderHistory(sessionId);
  try {
    sessionStorage.setItem(SESSION_KEY, sessionId);
  } catch {
    setText('chat-feedback', 'Sesja działa, ale nie może zostać zapisana w karcie przeglądarki.');
  }
}

function showView(view) {
  const ids = { chat: 'chat-view', work: 'work-view', 'ai-center': 'ai-center-view', codex: 'codex-view', github: 'github-view', terminal: 'terminal-view', settings: 'settings-view' };
  for (const [name, id] of Object.entries(ids)) {
    const section = document.getElementById(id);
    if (section) section.hidden = name !== view;
  }
  for (const button of document.querySelectorAll('[data-view]')) {
    const active = button.dataset.view === view;
    button.classList.toggle('is-active', active);
    if (button.classList.contains('nav-button')) {
      if (active) button.setAttribute('aria-current', 'page');
      else button.removeAttribute('aria-current');
    } else {
      button.setAttribute('aria-pressed', String(active));
    }
  }
  const titles = { chat: syncedUiTitle, work: 'Work', 'ai-center': 'Centrum AI 24/7', codex: 'Centrum Programowania Codex', github: 'GitHub · MCP', terminal: 'Terminal', settings: 'Status systemu' };
  setText('page-title', titles[view] ?? titles.chat);
}

function setMobileSidebar(open) {
  const sidebar = document.getElementById('sidebar');
  const menu = document.getElementById('mobile-menu');
  const backdrop = document.getElementById('sidebar-backdrop');
  if (!sidebar || !menu || !backdrop) return;
  const isMobile = window.matchMedia('(max-width: 760px)').matches;
  sidebar.classList.toggle('is-open', isMobile && open);
  sidebar.inert = isMobile && !open;
  sidebar.setAttribute('aria-hidden', String(isMobile && !open));
  menu.setAttribute('aria-expanded', String(isMobile && open));
  menu.setAttribute('aria-label', isMobile && open ? 'Zamknij panel boczny' : 'Otwórz panel boczny');
  backdrop.hidden = !(isMobile && open);
  document.body.classList.toggle('sidebar-open', isMobile && open);
}

function setCollapsedSidebar(collapsed) {
  const layout = document.getElementById('app-layout');
  const button = document.getElementById('sidebar-collapse');
  if (!layout || !button) return;
  layout.dataset.collapsed = String(collapsed);
  button.setAttribute('aria-expanded', String(!collapsed));
  button.setAttribute('aria-label', collapsed ? 'Rozwiń panel boczny' : 'Zwiń panel boczny');
  button.title = collapsed ? 'Rozwiń panel boczny' : 'Zwiń panel boczny';
}

function bindNavigation() {
  const menu = document.getElementById('mobile-menu');
  const backdrop = document.getElementById('sidebar-backdrop');
  const collapse = document.getElementById('sidebar-collapse');
  const sidebar = document.getElementById('sidebar');
  const media = window.matchMedia('(max-width: 760px)');
  menu?.addEventListener('click', () => setMobileSidebar(!sidebar?.classList.contains('is-open')));
  backdrop?.addEventListener('click', () => setMobileSidebar(false));
  collapse?.addEventListener('click', () => {
    const collapsed = document.getElementById('app-layout')?.dataset.collapsed === 'true';
    setCollapsedSidebar(!collapsed);
    try {
      localStorage.setItem('debian13-sidebar-collapsed', String(!collapsed));
    } catch {
      // The panel remains usable when browser storage is disabled.
    }
  });
  document.querySelectorAll('[data-view]').forEach((button) => {
    button.addEventListener('click', () => {
      showView(button.dataset.view);
      if (button.dataset.view === 'work') void refreshWork();
      if (button.dataset.view === 'codex') void loadCodexStatus();
      if (button.dataset.view === 'github') void refreshGithub();
      if (button.dataset.view === 'ai-center') void loadAiCenterStatus();
      setMobileSidebar(false);
    });
  });
  document.getElementById('new-chat')?.addEventListener('click', () => {
    activeSessionId = '';
    try {
      sessionStorage.removeItem(SESSION_KEY);
    } catch {
      // Starting a chat does not depend on session storage.
    }
    renderHistory();
    renderTranscriptMessages([]);
    showView('chat');
    setText('chat-feedback', 'Nowa sesja zostanie utworzona przez API po wysłaniu wiadomości.');
    document.getElementById('chat-message')?.focus();
    setMobileSidebar(false);
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && sidebar?.classList.contains('is-open')) {
      setMobileSidebar(false);
      menu?.focus();
    }
  });
  media.addEventListener('change', () => setMobileSidebar(false));
  try {
    setCollapsedSidebar(localStorage.getItem('debian13-sidebar-collapsed') === 'true');
  } catch {
    setCollapsedSidebar(false);
  }
  setMobileSidebar(false);
}

async function prepareVisionImage(file) {
  if (!['image/jpeg', 'image/png'].includes(file?.type)) {
    throw new ApiError('Vision obsługuje tylko obrazy JPEG i PNG.');
  }
  if (file.size > 8 * 1024 * 1024) {
    throw new ApiError('Zdjęcie przekracza limit 8 MB. Wybierz mniejszy plik.');
  }
  const objectUrl = URL.createObjectURL(file);
  try {
    const image = new Image();
    await new Promise((resolve, reject) => {
      image.onload = resolve;
      image.onerror = () => reject(new ApiError('Nie można odczytać wybranego zdjęcia.'));
      image.src = objectUrl;
    });
    const width = image.naturalWidth;
    const height = image.naturalHeight;
    if (!width || !height) throw new ApiError('Obraz nie zawiera poprawnych wymiarów.');
    const scale = Math.min(1, 1280 / Math.max(width, height));
    const canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.round(width * scale));
    canvas.height = Math.max(1, Math.round(height * scale));
    const context = canvas.getContext('2d');
    if (!context) throw new ApiError('Przeglądarka nie umożliwia przetworzenia zdjęcia.');
    context.fillStyle = '#ffffff';
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.drawImage(image, 0, 0, canvas.width, canvas.height);
    const uri = canvas.toDataURL('image/jpeg', 0.78);
    const encoded = uri.split(',', 2)[1] ?? '';
    if (!encoded || encoded.length > 4 * Math.ceil((2 * 1024 * 1024) / 3)) {
      throw new ApiError('Obraz po przetworzeniu jest za duży. Wybierz mniejszy plik.');
    }
    return { mime_type: 'image/jpeg', base64: encoded };
  } finally {
    URL.revokeObjectURL(objectUrl);
  }
}

function bindChatForm() {
  // Dialog Triady: independent, UI-only chat controller, shared authenticated API.
  // The agent dropdown selects a profile, not proof of an autonomous worker.
  const form = document.getElementById('chat-form');
  const message = document.getElementById('chat-message');
  const fileInput = document.getElementById('chat-image');
  const preview = document.getElementById('chat-image-preview');
  const previewImage = document.getElementById('chat-image-thumbnail');
  const previewName = document.getElementById('chat-image-name');
  const feedback = document.getElementById('chat-feedback');
  const submit = document.getElementById('chat-submit');
  const stop = document.getElementById('chat-stop');
  if (!form || !message || !fileInput || !feedback || !submit || !stop) return;

  let filePreviewUrl = '';
  let visionBusy = false;
  const clearPhoto = () => {
    if (filePreviewUrl) URL.revokeObjectURL(filePreviewUrl);
    filePreviewUrl = '';
    fileInput.value = '';
    if (previewImage) previewImage.removeAttribute('src');
    if (preview) preview.hidden = true;
    if (previewName) previewName.textContent = '';
  };
  const showError = (error, label = 'Błąd Dialogu Triady') => {
    const detail = typeof error?.message === 'string' ? error.message : 'Nie udało się wykonać operacji.';
    appendTranscript('error', label, detail);
    feedback.textContent = detail;
  };
  const setBusy = (busy, canStop = false) => {
    submit.disabled = busy;
    submit.hidden = busy && canStop;
    stop.hidden = !(busy && canStop);
    if (!busy) {
      stop.disabled = false;
      stop.textContent = 'Zatrzymaj';
    }
  };

  fileInput.addEventListener('change', () => {
    clearPhotoPreview();
    const file = fileInput.files?.[0];
    if (!file) return;
    if (!['image/jpeg', 'image/png'].includes(file.type) || file.size > 8 * 1024 * 1024) {
      fileInput.value = '';
      feedback.textContent = 'Real Vision obsługuje zdjęcia JPEG/PNG do 8 MB.';
      return;
    }
    filePreviewUrl = URL.createObjectURL(file);
    if (previewImage) previewImage.src = filePreviewUrl;
    if (previewName) previewName.textContent = file.name;
    if (preview) preview.hidden = false;
    feedback.textContent = 'Zdjęcie jest gotowe. Możesz dodać pytanie lub wysłać je bez tekstu.';
  });
  function clearPhotoPreview() {
    if (filePreviewUrl) URL.revokeObjectURL(filePreviewUrl);
    filePreviewUrl = '';
    if (previewImage) previewImage.removeAttribute('src');
    if (preview) preview.hidden = true;
  }
  document.getElementById('chat-image-remove')?.addEventListener('click', clearPhoto);
  message.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      form.requestSubmit();
    }
  });

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (activeChatController || visionBusy) return;
    const agentId = document.getElementById('chat-agent')?.value;
    if (!AGENT_PROFILES.some(({ id }) => id === agentId)) {
      feedback.textContent = 'Wybierz profil agenta.';
      return;
    }
    const text = message.value.trim();
    const photo = fileInput.files?.[0] ?? null;
    if (!text && !photo) {
      feedback.textContent = 'Napisz wiadomość albo dodaj zdjęcie.';
      return;
    }
    if (text.length > 4000) {
      feedback.textContent = 'Wiadomość przekracza limit 4000 znaków API.';
      return;
    }

    const name = AGENT_PROFILES.find(({ id }) => id === agentId)?.name ?? agentId;
    const prompt = text || 'Opisz dokładnie zdjęcie.';
    const previousSession = activeSessionId;
    appendTranscript('user', `Ty · ${name}${photo ? ' · Real Vision' : ''}`,
      photo ? `${prompt} [dołączono zdjęcie]` : prompt);
    const bubble = appendTranscript('assistant', `${name} · łączenie z modelem`, '');
    setBusy(true, !photo);

    if (photo) {
      visionBusy = true;
      feedback.textContent = 'Real Vision: przygotowywanie zdjęcia do Ollamy…';
      try {
        const encoded = await prepareVisionImage(photo);
        feedback.textContent = 'Real Vision: oczekiwanie na odpowiedź modelu…';
        const response = await api.vision(agentId, prompt, encoded, previousSession);
        if (typeof response.content !== 'string' || !response.content.trim()) {
          throw new ApiError('Model Vision zwrócił pustą odpowiedź.');
        }
        if (bubble) {
          bubble.text.textContent = response.content;
          bubble.heading.textContent = `${name} · ${response.model || 'Real Vision'}`;
        }
        if (response.sessionId && SESSION_ID_PATTERN.test(response.sessionId)) {
          activeSessionId = response.sessionId;
          recordHistory(response.sessionId, prompt);
        }
        message.value = '';
        clearPhoto();
        feedback.textContent = 'Analiza obrazu zakończona. Odpowiedź zapisana w historii.';
      } catch (error) {
        bubble?.entry.remove();
        showError(error, 'Błąd Real Vision');
      } finally {
        visionBusy = false;
        setBusy(false);
        void loadTasks();
      }
      return;
    }

    const controller = new AbortController();
    activeChatController = controller;
    activeChatTaskId = '';
    activeChatCancelMessage = '';
    feedback.textContent = 'Dialog Triady: oczekiwanie na model…';
    let answer = '';
    let done = false;
    let cancelled = false;
    let responseSession = '';
    let source = '';
    let replyName = name;

    try {
      await api.chatStream(prompt, agentId, previousSession, ({ event: kind, payload }) => {
        if (kind === 'meta') {
          if (typeof payload.session_id === 'string') responseSession = payload.session_id;
          if (typeof payload.task_id === 'string' && SESSION_ID_PATTERN.test(payload.task_id)) {
            activeChatTaskId = payload.task_id;
          }
        } else if (kind === 'delta' && typeof payload.text === 'string') {
          answer += payload.text;
          if (bubble) bubble.text.textContent = answer;
          const transcript = document.getElementById('chat-transcript');
          if (transcript) transcript.scrollTop = transcript.scrollHeight;
        } else if (kind === 'ui_update') {
          // This is an authenticated operator command, not proof of AI inference.
          void refreshSyncState();
        } else if (kind === 'done') {
          done = true;
          if (typeof payload.session_id === 'string') responseSession = payload.session_id;
          source = typeof payload.source === 'string' ? payload.source : 'API';
          replyName = AGENT_PROFILES.find(({ id }) => id === payload.agent)?.name ?? name;
        } else if (kind === 'cancelled') {
          cancelled = true;
        } else if (kind === 'error') {
          throw new ApiError(typeof payload.error === 'string' ? payload.error : 'Błąd strumienia.');
        }
      }, controller.signal);
      if (!done && !cancelled) throw new ApiError('Serwer nie potwierdził zakończenia odpowiedzi.');
      if (cancelled) {
        if (bubble) bubble.heading.textContent = `${name} · anulowano`;
        feedback.textContent = 'API potwierdziło anulowanie.';
      } else {
        if (bubble) bubble.heading.textContent = `${replyName} · ${source}`;
        if (responseSession && SESSION_ID_PATTERN.test(responseSession)) {
          activeSessionId = responseSession;
          recordHistory(responseSession, prompt);
        }
        message.value = '';
        feedback.textContent = source === 'system_command'
          ? 'Polecenie systemowe zapisane i zsynchronizowane. To nie była inferencja AI.'
          : 'Odpowiedź z modelu zapisana w historii Dialogu Triady.';
      }
    } catch (error) {
      if (error?.name === 'AbortError') {
        if (bubble) bubble.heading.textContent = `${name} · transmisja przerwana`;
        feedback.textContent = activeChatCancelMessage ||
          'Przerwano odbiór. Anulowanie działania modelu nie zostało potwierdzone.';
      } else {
        if (!answer) bubble?.entry.remove();
        else if (bubble) bubble.heading.textContent = `${name} · niepełna odpowiedź`;
        showError(error);
      }
    } finally {
      activeChatController = null;
      activeChatTaskId = '';
      setBusy(false);
      void loadTasks();
    }
  });

  stop.addEventListener('click', async () => {
    const controller = activeChatController;
    if (!controller || stop.disabled) return;
    const taskId = activeChatTaskId;
    if (!taskId) {
      activeChatCancelMessage = 'Przerwano odbiór lokalnie; backend nie potwierdził anulowania.';
      controller.abort();
      return;
    }
    stop.disabled = true;
    stop.textContent = 'Anulowanie…';
    try {
      const response = await api.cancelTask(taskId);
      if (response.task?.id !== taskId) throw new ApiError('Brak potwierdzenia anulowania.');
      feedback.textContent = `API przyjęło anulowanie (${response.task.status}).`;
    } catch (error) {
      activeChatCancelMessage = 'Nie potwierdzono anulowania przez API; odbiór przerwany lokalnie.';
      controller.abort();
      feedback.textContent = error.message || activeChatCancelMessage;
    }
  });
}


function bindAgentDialog() {
  const form = document.getElementById('agent-dialog-form');
  const feed = document.getElementById('agent-dialog-feed');
  const input = document.getElementById('agent-dialog-content');
  const recipient = document.getElementById('agent-dialog-recipient');
  const state = document.getElementById('agent-dialog-state');
  const send = document.getElementById('agent-dialog-send');
  if (!form || !feed || !input || !recipient || !state || !send) return;
  let lastId = 0;
  let busy = false;
  let loading = false;

  const nameOf = (id) => ({
    operator: 'Ty', all: 'Wszyscy',
    aria: 'Aria', alex: 'Alex', astyria: 'Astyria', aurelius: 'Aurelius',
  }[id] || id);

  async function refreshDialog() {
    if (loading) return;
    loading = true;
    try {
      const data = await api.getAgentDialog(lastId);
      if (!Array.isArray(data.messages)) throw new ApiError('Nieprawidłowa lista wiadomości AI.');
      for (const item of data.messages) {
        if (!Number.isSafeInteger(item.id) || item.id <= lastId) continue;
        if (typeof item.content !== 'string' || typeof item.sender !== 'string') continue;
        if (lastId === 0 && feed.querySelector('.empty-state')) feed.replaceChildren();
        const article = document.createElement('article');
        article.className = 'transcript-entry transcript-' + (item.sender === 'operator' ? 'user' : 'assistant');
        const heading = document.createElement('p');
        heading.className = 'transcript-heading';
        heading.textContent = `${nameOf(item.sender)} → ${nameOf(item.recipient)} · API (wiadomość, nie dowód inferencji)`;
        const content = document.createElement('p');
        content.className = 'transcript-text';
        content.textContent = item.content;
        article.append(heading, content);
        feed.append(article);
        lastId = item.id;
      }
      if (Number.isSafeInteger(data.lastId)) lastId = Math.max(lastId, data.lastId);
      state.textContent = 'Kanał dostępny · odpowiedzi agentów są dobrowolne; brak automatycznego wywoływania modeli.';
      feed.scrollTop = feed.scrollHeight;
    } catch (error) {
      state.textContent = error.message || 'Kanał AI jest niedostępny.';
    } finally {
      loading = false;
    }
  }

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (busy) return;
    const content = input.value.trim();
    if (!content) return;
    busy = true;
    send.disabled = true;
    try {
      const result = await api.postAgentDialog(recipient.value, content);
      if (result.accepted !== true) throw new ApiError('API nie potwierdziło zapisu.');
      input.value = '';
      await refreshDialog();
    } catch (error) {
      state.textContent = error.message || 'Wysłanie wiadomości nie powiodło się.';
    } finally {
      busy = false;
      send.disabled = false;
    }
  });
  document.getElementById('agent-dialog-refresh')?.addEventListener('click', refreshDialog);
  void refreshDialog();
  window.setInterval(() => {
    if (document.visibilityState === 'visible') void refreshDialog();
  }, 5000);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') void refreshDialog();
  });
}

function bindWorkCreateForm() {
  const form = document.getElementById('work-create-form');
  if (!form) return;
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = document.getElementById('work-create-submit');
    const agent = document.getElementById('work-task-agent')?.value;
    const kind = document.getElementById('work-task-kind')?.value;
    const title = document.getElementById('work-task-title')?.value.trim();
    const description = document.getElementById('work-task-description')?.value.trim();
    if (!agent || !kind || !title || !description || button.disabled) return;
    button.disabled = true;
    setText('work-create-feedback', 'Zapisuję zadanie w bazie API…');
    try {
      const result = await api.createTask({ agent, kind, title, description });
      if (!result.task?.id || result.executionStarted !== false) {
        throw new ApiError('API nie potwierdziło bezpiecznej rejestracji zadania.');
      }
      document.getElementById('work-task-title').value = '';
      document.getElementById('work-task-description').value = '';
      await refreshWork();
      setText('work-create-feedback', 'Zadanie zapisane. Agent AI NIE został uruchomiony; oczekuje na przegląd operatora.');
    } catch (error) {
      setText('work-create-feedback', error.message);
    } finally {
      button.disabled = false;
    }
  });
}

function bindTerminalForm() {
  const form = document.getElementById('terminal-form');
  if (!form) return;
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const commandInput = document.getElementById('terminal-command');
    const submit = document.getElementById('terminal-submit');
    const feedback = document.getElementById('terminal-feedback');
    const command = commandInput?.value.trim();
    if (!command || !feedback) return;
    submit.disabled = true;
    feedback.textContent = 'Wysyłanie do ograniczonego API terminala…';
    try {
      const result = await api.terminal(command);
      if (typeof result.output !== 'string') throw new ApiError('API nie zwróciło wyniku tekstowego.');
      renderTerminal(command, result.output);
      commandInput.value = '';
      feedback.textContent = 'Polecenie obsłużone bez uruchamiania powłoki.';
    } catch (error) {
      renderTerminal(command, error.message, true);
      feedback.textContent = error.message;
    } finally {
      submit.disabled = false;
      await loadTasks();
    }
  });
}

if (typeof document !== 'undefined') {
  renderAgentProfiles(document.getElementById('agent-list'));
  bindNavigation();
  bindChatForm();
  bindAgentDialog();
  bindTerminalForm();
  bindWorkCreateForm();
  renderHistory();
  document.getElementById('global-sync')?.addEventListener('click', synchronizeAllData);
  document.getElementById('refresh-status')?.addEventListener('click', refreshDashboard);
  document.getElementById('codex-refresh')?.addEventListener('click', loadCodexStatus);
  document.getElementById('ai-center-refresh')?.addEventListener('click', loadAiCenterStatus);
  document.getElementById('refresh-tasks')?.addEventListener('click', refreshWork);
  document.getElementById('github-refresh')?.addEventListener('click', refreshGithub);
  document.getElementById('sync-example')?.addEventListener('click', () => {
    const textarea = document.getElementById('chat-message');
    if (textarea) {
      textarea.value = '/panel tytuł Moje Centrum AI';
      textarea.focus();
    }
  });
  void refreshSyncState();
  window.setInterval(() => {
    if (document.visibilityState === 'visible') void refreshSyncState();
  }, 5000);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') void refreshSyncState();
  });
  void refreshDashboard();
  let savedSession = '';
  try {
    savedSession = sessionStorage.getItem(SESSION_KEY) ?? '';
  } catch {
    // The API-backed chat remains available without browser storage.
  }
  if (SESSION_ID_PATTERN.test(savedSession)) void loadHistory(savedSession);
}
