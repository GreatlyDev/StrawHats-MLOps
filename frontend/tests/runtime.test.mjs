import test from 'node:test';
import assert from 'node:assert/strict';
import { resolveRuntime, selectRequest } from '../src/runtime.ts';

test('workspace is the default and retains its configured API origin', () => {
  assert.deepEqual(resolveRuntime(), { mode: 'workspace', apiBase: '' });
  assert.deepEqual(resolveRuntime('', 'https://example.test/'), {
    mode: 'workspace',
    apiBase: 'https://example.test/',
  });
  assert.equal(resolveRuntime('workspace').mode, 'workspace');
});

test('portfolio must be explicit and cannot also configure a backend', () => {
  assert.deepEqual(resolveRuntime('portfolio'), {
    mode: 'portfolio',
    apiBase: '',
  });
  for (const value of ['demo', 'production', 'Portfolio', ' ']) {
    assert.throws(() => resolveRuntime(value), /VITE_APP_MODE/);
  }
  for (const base of ['https://example.test', '/backend', ' ']) {
    assert.throws(() => resolveRuntime('portfolio', base), /VITE_API_BASE_URL/);
  }
});

test('an unavailable workspace remains an error without a portfolio fallback', async () => {
  let portfolioCalls = 0;
  const http = async () => {
    throw new Error('Cannot reach the API');
  };
  const portfolio = async () => {
    portfolioCalls++;
    return ['prepared model'];
  };
  const request = selectRequest(resolveRuntime(), http, portfolio);
  await assert.rejects(request('/models'), /Cannot reach the API/);
  assert.equal(portfolioCalls, 0);
  assert.equal(request, http);
});

test('portfolio selects only its browser adapter for every request', async () => {
  const http = async () => {
    throw new Error('HTTP must not run');
  };
  const portfolio = async (path) => ({ path, source: 'browser' });
  const request = selectRequest(resolveRuntime('portfolio'), http, portfolio);
  assert.equal(request, portfolio);
  assert.deepEqual(await request('/overview'), {
    path: '/overview',
    source: 'browser',
  });
});
