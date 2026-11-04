import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { readWorkspaceSources, readWithTimeout } from './global_sync.mjs';

const repositoryRoot = fileURLToPath(new URL('../../', import.meta.url));
const TEST_SESSION_ID = '123e4567-e89b-42d3-a456-426614174000';
const TEST_TASK_ID = '123e4567-e89b-42d3-a456-426614174001';

async function reserveLoopbackPort() {
  const server = createServer();
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  const { port } = server.address();
  await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  return port;
}

async function startUpstreamDouble(t) {
  const requests = [];
  const server = createServer(async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    requests.push({
      method: request.method,
      url: request.url,
      contentType: request.headers['content-type'] ?? null,
      authorization: request.headers.authorization ?? null,
      body: Buffer.concat(chunks).toString('utf8'),
    });
    if (request.url === '/api/v1/chat/stream') {
      response.writeHead(200, {
        'Content-Type': 'text/event-stream; charset=utf-8',
        'X-Upstream-Header': 'must-not-be-relayed',
      });
      response.write('event: meta\ndata: {"session_id":"123e4567-e89b-42d3-a456-426614174000"}\n\n');
      setTimeout(() => {
        response.end('event: done\ndata: {"source":"local_command"}\n\n');
      }, 100);
      return;
    }
    response.writeHead(200, {
      'Content-Type': 'text/html; charset=utf-8',
      'X-Upstream-Header': 'must-not-be-relayed',
    });
    response.end(JSON.stringify({ ok: true, path: request.url }));
  });
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  t.after(() => new Promise((resolve, reject) =>
    server.close((error) => error ? reject(error) : resolve())));
  return { port: server.address().port, requests };
}

async function startUiProxy(t, apiPort, apiToken = '') {
  const port = await reserveLoopbackPort();
  const child = spawn('python3', ['debian13/ui/dev_server.py'], {
    cwd: repositoryRoot,
    env: {
      ...process.env,
      DEBIAN13_API_PORT: String(apiPort),
      DEBIAN13_API_TOKEN: apiToken,
      DEBIAN13_UI_PORT: String(port),
    },
    stdio: 'ignore',
  });
  t.after(async () => {
    if (child.exitCode !== null || child.signalCode !== null) return;
    const exited = once(child, 'exit');
    child.kill('SIGTERM');
    let timeout;
    await Promise.race([
      exited,
      new Promise((resolve) => {
        timeout = setTimeout(resolve, 2000);
      }),
    ]);
    clearTimeout(timeout);
  });

  const baseUrl = `http://127.0.0.1:${port}`;
  let lastError;
  for (let attempt = 0; attempt < 40; attempt += 1) {
    if (child.exitCode !== null) throw new Error(`UI proxy exited with ${child.exitCode}`);
    try {
      const response = await fetch(`${baseUrl}/`, { signal: AbortSignal.timeout(500) });
      if (response.ok) return baseUrl;
    } catch (error) {
      lastError = error;
    }
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  throw new Error(`UI proxy did not start: ${lastError?.message ?? 'unknown error'}`);
}

test('real loopback proxy allows only contract routes and normalizes upstream headers', async (t) => {
  const upstream = await startUpstreamDouble(t);
  const baseUrl = await startUiProxy(t, upstream.port);

  const status = await fetch(`${baseUrl}/api/v1/status`);
  assert.equal(status.status, 200);
  assert.equal(status.headers.get('content-type'), 'application/json; charset=utf-8');
  assert.equal(status.headers.get('x-upstream-header'), null);
  assert.equal(status.headers.get('content-security-policy')?.includes("default-src 'self'"), true);
  assert.deepEqual(await status.json(), { ok: true, path: '/api/v1/status' });
  assert.equal(upstream.requests[0].authorization, null, 'the proxy must not forward a browser-supplied token by default');

  const chat = await fetch(`${baseUrl}/api/v1/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message: '/help', agent: 'aria' }),
  });
  assert.equal(chat.status, 200);
  assert.equal((await chat.json()).path, '/api/v1/chat');
  assert.equal(upstream.requests[1].contentType, 'application/json');
  assert.deepEqual(JSON.parse(upstream.requests[1].body), { message: '/help', agent: 'aria' });

  const stream = await fetch(`${baseUrl}/api/v1/chat/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message: '/help', agent: 'aria' }),
  });
  assert.equal(stream.status, 200);
  assert.equal(stream.headers.get('content-type'), 'text/event-stream; charset=utf-8');
  assert.equal(stream.headers.get('x-upstream-header'), null);
  const reader = stream.body.getReader();
  const firstEvent = await reader.read();
  assert.equal(firstEvent.done, false, 'SSE headers and first event should arrive before upstream closes');
  assert.match(new TextDecoder().decode(firstEvent.value), /event: meta/);
  let remainingText = '';
  let streamDone = false;
  while (!streamDone) {
    const next = await reader.read();
    streamDone = next.done;
    if (next.value) remainingText += new TextDecoder().decode(next.value);
  }
  assert.match(remainingText, /event: done/);

  const history = await fetch(`${baseUrl}/api/v1/sessions/${TEST_SESSION_ID}/messages`);
  assert.equal(history.status, 200);
  assert.equal((await history.json()).path, `/api/v1/sessions/${TEST_SESSION_ID}/messages`);

  const events = await fetch(`${baseUrl}/api/v1/events`);
  assert.equal(events.status, 200);
  assert.deepEqual(await events.json(), { ok: true, path: '/api/v1/events' });

  const task = await fetch(`${baseUrl}/api/v1/tasks/${TEST_TASK_ID}`);
  assert.equal(task.status, 200);
  assert.deepEqual(await task.json(), { ok: true, path: `/api/v1/tasks/${TEST_TASK_ID}` });

  const denied = await fetch(`${baseUrl}/api/v1/agents/extra`);
  assert.equal(denied.status, 404);
  assert.deepEqual(await denied.json(), { error: 'Nie znaleziono endpointu API.' });
  const invalidSession = await fetch(`${baseUrl}/api/v1/sessions/not-a-uuid/messages`);
  assert.equal(invalidSession.status, 404);
  const invalidTask = await fetch(`${baseUrl}/api/v1/tasks/not-a-uuid`);
  assert.equal(invalidTask.status, 404);
  assert.equal(upstream.requests.length, 6, 'disallowed paths must not reach the upstream API');
});

test('real loopback proxy injects only its configured operator token and forwards bodyless cancel', async (t) => {
  const upstream = await startUpstreamDouble(t);
  const fixtureToken = 'fixture-only-operator-token';
  const baseUrl = await startUiProxy(t, upstream.port, fixtureToken);

  const response = await fetch(`${baseUrl}/api/v1/tasks/${TEST_TASK_ID}/cancel`, {
    method: 'POST',
    headers: { Authorization: ['Bear', 'er browser-supplied-token'].join('') },
  });
  assert.equal(response.status, 200);
  assert.equal((await response.json()).path, `/api/v1/tasks/${TEST_TASK_ID}/cancel`);
  assert.equal(upstream.requests.length, 1);
  assert.equal(upstream.requests[0].authorization, ['Bear', 'er fixture-only-operator-token'].join(''));
  assert.equal(upstream.requests[0].contentType, null);
  assert.equal(upstream.requests[0].body, '');
});

test('real loopback proxy rejects a body on task cancellation before forwarding', async (t) => {
  const upstream = await startUpstreamDouble(t);
  const baseUrl = await startUiProxy(t, upstream.port);
  const response = await fetch(`${baseUrl}/api/v1/tasks/${TEST_TASK_ID}/cancel`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}',
  });
  assert.equal(response.status, 400);
  assert.deepEqual(await response.json(), { error: 'Anulowanie zadania nie przyjmuje treści.' });
  assert.equal(upstream.requests.length, 0);
});

test('real loopback proxy rejects oversized writes before forwarding', async (t) => {
  const upstream = await startUpstreamDouble(t);
  const baseUrl = await startUiProxy(t, upstream.port);
  const response = await fetch(`${baseUrl}/api/v1/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: 'x'.repeat(16_385),
  });
  assert.equal(response.status, 413);
  assert.deepEqual(await response.json(), { error: 'Żądanie przekracza limit rozmiaru.' });
  assert.equal(upstream.requests.length, 0);
});

test('integrations panel proxy maps authenticated UI route to the real backend API', async (t) => {
  const upstream = await startUpstreamDouble(t);
  const baseUrl = await startUiProxy(t, upstream.port, 'fixture-only-operator-token');
  const response = await fetch(`${baseUrl}/api/v1/integrations`);
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { ok: true, path: '/v1/integrations' });
  assert.equal(upstream.requests.length, 1);
  assert.equal(upstream.requests[0].url, '/v1/integrations');
  assert.equal(upstream.requests[0].authorization, 'Bearer fixture-only-operator-token');
});

test('persistent Work task creation is proxied with server-side token', async (t) => {
  const upstream = await startUpstreamDouble(t);
  const base = await startUiProxy(t, upstream.port, 'fixture-long-operator-token');
  const task = { agent: 'alex', kind: 'development', title: 'Debian 13',
    description: 'Plan only: no automatic workers' };
  const response = await fetch(`${base}/api/v1/tasks`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer should-not-be-forwarded' },
    body: JSON.stringify(task),
  });
  assert.equal(response.status, 200);
  assert.equal((await response.json()).path, '/api/v1/tasks');
  assert.equal(upstream.requests[0].method, 'POST');
  assert.equal(upstream.requests[0].authorization, 'Bearer fixture-long-operator-token');
  assert.deepEqual(JSON.parse(upstream.requests[0].body), task);
});

test('original Alex capability and explicit operator handoff stay behind tokenized UI proxy', async (t) => {
  const upstream = await startUpstreamDouble(t);
  const base = await startUiProxy(t, upstream.port, 'operator-test-token-long-enough-for-ci');
  const capability = await fetch(`${base}/api/v1/alex/capabilities`);
  assert.equal(capability.status, 200);
  assert.equal((await capability.json()).path, '/api/v1/alex/capabilities');
  const taskId = '123e4567-e89b-42d3-a456-426614174001';
  const dispatch = await fetch(`${base}/api/v1/tasks/${taskId}/dispatch`, {
    method: 'POST',
    headers: { 'Authorization': 'Bearer do-not-trust-browser' },
  });
  assert.equal(dispatch.status, 200);
  assert.equal((await dispatch.json()).path, `/api/v1/tasks/${taskId}/dispatch`);
  assert.equal(upstream.requests[0].authorization,
    'Bearer operator-test-token-long-enough-for-ci');
  assert.equal(upstream.requests[1].authorization,
    'Bearer operator-test-token-long-enough-for-ci');
  assert.equal(upstream.requests[1].body, '');
  assert.equal(upstream.requests[1].method, 'POST');
  const blocked = await fetch(`${base}/api/v1/tasks/${taskId}/dispatch`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({force: true, purchaseAuthorized: true}),
  });
  assert.equal(blocked.status, 400);
  assert.equal(upstream.requests.length, 2, 'invalid dispatch must not reach API');
});

test('Codex private diagnostic proxy is read-only and forwards only server token', async (t) => {
  const upstream = await startUpstreamDouble(t);
  const base = await startUiProxy(t, upstream.port, 'codex-private-test-token');
  const response = await fetch(`${base}/api/v1/codex/status`, {
    headers: {Authorization: 'Bearer browser-token-not-forwarded'},
  });
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), {ok: true, path: '/api/v1/codex/status'});
  assert.equal(upstream.requests.at(-1).authorization, 'Bearer codex-private-test-token');
  assert.equal(upstream.requests.at(-1).url, '/api/v1/codex/status');
  const denied = await fetch(`${base}/api/v1/codex/execute`, {
    method: 'POST', headers: {'Content-Type':'application/json'}, body:'{}',
  });
  assert.equal(denied.status, 404);
  assert.equal(upstream.requests.length, 1);
});

test('local Ollama approval route is authenticated, bodyless, and cannot execute code', async (t) => {
  const upstream = await startUpstreamDouble(t);
  const base = await startUiProxy(t, upstream.port, 'fixture-ai-center-operator-token');
  const status = await fetch(`${base}/api/v1/ai-center/status`);
  assert.equal(status.status, 200);
  assert.deepEqual(await status.json(), {ok: true, path: '/api/v1/ai-center/status'});
  const approved = await fetch(`${base}/api/v1/tasks/${TEST_TASK_ID}/approve-analysis`, {
    method: 'POST', headers: {Authorization: 'Bearer injected-browser-token'},
  });
  assert.equal(approved.status, 200);
  assert.equal((await approved.json()).path,
               `/api/v1/tasks/${TEST_TASK_ID}/approve-analysis`);
  assert.equal(upstream.requests[1].authorization,
               'Bearer fixture-ai-center-operator-token');
  assert.equal(upstream.requests[1].body, '');
  const denied = await fetch(`${base}/api/v1/tasks/${TEST_TASK_ID}/approve-analysis`, {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({root:true}),
  });
  assert.equal(denied.status, 400);
  assert.equal(upstream.requests.length, 2, 'local Ollama approval route must reject bodies');
});

test('integration progress is read-only and uses only the server-side token', async (t) => {
  const upstream = await startUpstreamDouble(t);
  const base = await startUiProxy(t, upstream.port, 'integration-progress-test-operator-token');
  const response = await fetch(`${base}/api/v1/integration-progress`, {
    headers: {Authorization: 'Bearer untrusted-browser-credential'},
  });
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), {ok: true, path: '/api/v1/integration-progress'});
  assert.equal(upstream.requests.length, 1);
  assert.equal(upstream.requests[0].authorization,
               'Bearer integration-progress-test-operator-token');
  const denied = await fetch(`${base}/api/v1/integration-progress`, {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}',
  });
  assert.equal(denied.status, 404);
  assert.equal(upstream.requests.length, 1, 'POST must never reach the runtime');
});

test('global sync verifies all ten read-only sources without claiming a deploy', async () => {
  const calls = [];
  const responses = {
    getStatus: {application: {status: 'running'}},
    getSyncState: {revision: 2, title: 'Debian13'},
    getTasks: {tasks: []},
    getEvents: {events: []},
    getGitHubStatus: {githubApiReachable: true, partial: false},
    getIntegrations: {integrations: []},
    getIntegrationProgress: {kind: 'local_integration_checklist', notOsCompletion: true, percentage: 30},
    getAiCenterStatus: {type: 'local_ollama_queue', workerHeartbeatVerified: false},
    getCodexStatus: {module: 'codex', cliInstalled: false},
    getOriginalAgents: {agents: ['Alex','Aria','Astyria','Aurelius']},
  };
  const client = Object.fromEntries(Object.entries(responses).map(([key, value]) => [
    key, async () => { calls.push(key); return value; },
  ]));
  client.deployCode = () => { throw new Error('Must never deploy source'); };
  client.dispatchAgent = () => { throw new Error('Must never start AI'); };
  const result = await readWorkspaceSources(client);
  assert.equal(result.total, 10);
  assert.equal(result.successful, 10);
  assert.deepEqual(result.failed, []);
  assert.equal(result.complete, true);
  assert.equal(result.readOnly, true);
  assert.equal(result.deployedCode, false);
  assert.equal(result.aiExecutionVerified, false);
  assert.equal(calls.length, 10);
  assert.equal(calls.every(name => name.startsWith('get')), true);
});

test('global sync finishes when a read-only source never responds', async () => {
  const client = {
    getStatus: async () => ({application: {status: 'running'}}),
    getSyncState: async () => ({revision: 0, title: 'Debian 13'}),
    getTasks: async () => ({tasks: []}),
    getEvents: async () => ({events: []}),
    getGitHubStatus: () => new Promise(() => {}), // Simulate stalled GitHub.
    getIntegrations: async () => ({integrations: []}),
    getIntegrationProgress: async () => ({
      kind: 'local_integration_checklist', notOsCompletion: true, percentage: 30,
    }),
    getAiCenterStatus: async () => ({
      type: 'local_ollama_queue', workerHeartbeatVerified: false,
    }),
    getCodexStatus: async () => ({module: 'codex', cliInstalled: false}),
    getOriginalAgents: async () => ({agents: ['Alex', 'Aria', 'Astyria', 'Aurelius']}),
  };
  const result = await readWorkspaceSources(client, {timeoutMs: 30});
  assert.equal(result.total, 10);
  assert.equal(result.successful, 9);
  assert.deepEqual(result.failed, ['GitHub']);
  assert.equal(result.complete, false);
  assert.equal(result.readOnly, true);
  assert.equal(result.deployedCode, false);
  assert.equal(result.aiExecutionVerified, false);
});

test('bounded dashboard refresh settles even when one view stalls', async () => {
  const calls = [];
  const results = await Promise.allSettled([
    () => { calls.push('status'); return Promise.resolve('ok'); },
    () => { calls.push('stalled'); return new Promise(() => {}); },
    () => { calls.push('work'); return Promise.resolve({ tasks: [] }); },
  ].map(operation => readWithTimeout(operation, 30)));
  assert.deepEqual(calls.sort(), ['stalled', 'status', 'work']);
  assert.equal(results.length, 3);
  assert.equal(results[0].status, 'fulfilled');
  assert.equal(results[0].value, 'ok');
  assert.equal(results[1].status, 'rejected');
  assert.equal(results[1].reason.message, 'Source read timed out');
  assert.equal(results[2].status, 'fulfilled');
  assert.deepEqual(results[2].value, {tasks: []});
  await assert.rejects(readWithTimeout(() => Promise.reject(new Error('unavailable')), 30),
    /unavailable/);
});

test('global sync reports partial API failure without leaking upstream secrets', async () => {
  const secret = 'secret-in-mock-network-error';
  const client = {
    getStatus: async () => ({application: {status: 'running'}}),
    getSyncState: async () => ({revision: 0, title: 'Panel'}),
    getTasks: async () => ({tasks: []}),
    getEvents: async () => ({events: []}),
    getGitHubStatus: async () => { throw new Error(secret); },
    getIntegrations: async () => ({integrations: []}),
    getIntegrationProgress: async () => ({kind: 'local_integration_checklist', notOsCompletion: true, percentage: 30}),
    getAiCenterStatus: async () => ({type: 'local_ollama_queue', workerHeartbeatVerified: false}),
    getCodexStatus: async () => ({module: 'codex', cliInstalled: false}),
    getOriginalAgents: async () => ({agents: []}),
  };
  const value = await readWorkspaceSources(client);
  assert.equal(value.complete, false);
  assert.equal(value.successful, 8);
  assert.deepEqual(value.failed, ['GitHub','Profile AI']);
  assert.equal(JSON.stringify(value).includes(secret), false);
  assert.equal(value.deployedCode, false);
  const noCodex = {...client};
  delete noCodex.getCodexStatus;
  const missingMethod = await readWorkspaceSources(noCodex);
  assert.equal(missingMethod.successful, 7);
  assert.deepEqual(missingMethod.failed, ['GitHub', 'Codex', 'Profile AI']);
});

test('global sync UI control and read-only JS are served locally', async (t) => {
  const upstream = await startUpstreamDouble(t);
  const base = await startUiProxy(t, upstream.port);
  const index = await fetch(base + '/');
  assert.equal(index.status, 200);
  const html = await index.text();
  assert.match(html, /id="global-sync"/);
  assert.match(html, /id="global-sync-status"/);
  assert.match(html, /Nie wdraża kodu/);
  const module = await fetch(base + '/global_sync.mjs');
  assert.equal(module.status, 200);
  assert.match(await module.text(), /readWorkspaceSources/);
  assert.equal(upstream.requests.length, 0);
});

test('SVL status proxy only allows read-only requests and never forwards browser token', async (t) => {
  const upstream = await startUpstreamDouble(t);
  const base = await startUiProxy(t, upstream.port, 'svl-operator-token-from-private-server');
  const result = await fetch(base + '/api/v1/svl/status', {
    headers: {Authorization: 'Bearer browser-untrusted-token'},
  });
  assert.equal(result.status, 200);
  assert.deepEqual(await result.json(), {ok: true, path: '/api/v1/svl/status'});
  assert.equal(upstream.requests.length, 1);
  assert.equal(upstream.requests[0].authorization, 'Bearer svl-operator-token-from-private-server');
  const attemptedWrite = await fetch(base + '/api/v1/svl/status', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action: 'bump', root: true}),
  });
  assert.equal(attemptedWrite.status, 404);
  assert.equal(upstream.requests.length, 1);
  const page = await fetch(base + '/');
  const html = await page.text();
  assert.match(html, /id="svl-status"/);
  assert.equal(html.includes('Polecenie <code>/svl</code>'), true);
});
