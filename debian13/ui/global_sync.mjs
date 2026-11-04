// Read-only global refresh: inspect real backend endpoints without running AI,
// deploying GitHub code, changing tasks, or fabricating successful connections.
const SOURCES = Object.freeze([
  ['API', 'getStatus', data => data?.application?.status === 'running'],
  ['Stan panelu', 'getSyncState', data => Number.isSafeInteger(data?.revision) && data.revision >= 0 && typeof data?.title === 'string'],
  ['Zadania', 'getTasks', data => Array.isArray(data?.tasks)],
  ['Zdarzenia', 'getEvents', data => Array.isArray(data?.events)],
  ['GitHub', 'getGitHubStatus', data => data?.githubApiReachable === true && data?.partial === false],
  ['Integracje', 'getIntegrations', data => Array.isArray(data?.integrations)],
  ['Procenty', 'getIntegrationProgress', data => data?.kind === 'local_integration_checklist' && data?.notOsCompletion === true && Number.isInteger(data?.percentage)],
  ['Centrum AI', 'getAiCenterStatus', data => data?.type === 'local_ollama_queue' && typeof data?.workerHeartbeatVerified === 'boolean'],
  ['Codex', 'getCodexStatus', data => data?.module === 'codex' && typeof data?.cliInstalled === 'boolean'],
  ['Profile AI', 'getOriginalAgents', data => Array.isArray(data?.agents) && data.agents.length === 4],
]);

const SOURCE_TIMEOUT_MS = 10000;

// Shared bounded read for provider probes and view refreshes. Does not abort
// an underlying request; callers must never interpret a timeout as cancellation.
export async function readWithTimeout(operation, timeoutMs = SOURCE_TIMEOUT_MS) {
  if (typeof operation !== 'function') throw new TypeError('Read operation required');
  const limit = Number.isSafeInteger(timeoutMs) && timeoutMs > 0 && timeoutMs <= 30000
    ? timeoutMs : SOURCE_TIMEOUT_MS;
  let timer;
  try {
    return await Promise.race([
      Promise.resolve().then(operation),
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error('Source read timed out')), limit);
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

export async function readWorkspaceSources(client, { timeoutMs = SOURCE_TIMEOUT_MS } = {}) {
  // A hung source must not prevent the Synchronizuj button from completing.
  // Timeouts affect only read-only status checks; no deploy/agent action is started.
  const outcomes = await Promise.allSettled(SOURCES.map(async ([, method]) => {
    if (typeof client?.[method] !== 'function') {
      throw new Error('Endpoint not implemented');
    }
    return readWithTimeout(() => client[method](), timeoutMs);
  }));
  const succeeded = [];
  const failed = [];
  for (let i = 0; i < SOURCES.length; i += 1) {
    const [name, , validate] = SOURCES[i];
    const response = outcomes[i];
    if (response.status === 'fulfilled' && validate(response.value)) {
      succeeded.push(name);
    } else {
      // Never echo upstream exceptions, network URLs, headers or credentials.
      failed.push(name);
    }
  }
  return Object.freeze({
    total: SOURCES.length,
    successful: succeeded.length,
    succeeded,
    failed,
    complete: failed.length === 0,
    readOnly: true,
    deployedCode: false,
    aiExecutionVerified: false,
  });
}
