import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';
import { resolveOwnerTarget, createOwnerRequest } from '../src/owner-target.ts';
import { createPortfolioRequest } from '../src/portfolio/workspace.ts';

test('only exact owner kind and prefix pairs enable a target request', async () => {
  const seen = [];
  for (const [kind, prefix] of [
    ['local', '/local-iris'],
    ['eks', '/eks-iris'],
    ['rehearsal', '/rehearsal-iris'],
  ]) {
    const scope = createOwnerRequest(
      resolveOwnerTarget({ kind, api_prefix: prefix }),
      async (path) => {
        seen.push(path);
        return { ok: true };
      },
    );
    assert.deepEqual(await scope.request('/scale', { replicas: 2 }), {
      ok: true,
    });
    scope.close();
  }
  assert.deepEqual(seen, [
    '/local-iris/scale',
    '/eks-iris/scale',
    '/rehearsal-iris/scale',
  ]);
  for (const value of [
    null,
    {},
    { kind: 'eks', api_prefix: '/local-iris' },
    { kind: 'local', api_prefix: 'https://foreign' },
    { kind: 'other', api_prefix: '/eks-iris' },
  ]) {
    const scope = createOwnerRequest(resolveOwnerTarget(value), async () =>
      assert.fail('Disabled target reached HTTP'),
    );
    await assert.rejects(
      scope.request('/deploy', { replicas: 1 }),
      /unavailable|disabled/i,
    );
  }
});

test('a closed old target cannot deliver a delayed result to the new target', async () => {
  let finish;
  let oldSignal;
  const old = createOwnerRequest(
    { kind: 'local', api_prefix: '/local-iris' },
    async (_, __, signal) => {
      oldSignal = signal;
      return new Promise((resolve) => {
        finish = resolve;
      });
    },
  );
  const pending = old.request('/predict', { features: [5.1, 3.5, 1.4, 0.2] });
  old.close();
  assert.equal(oldSignal.aborted, true);
  const current = createOwnerRequest(
    { kind: 'eks', api_prefix: '/eks-iris' },
    async (path) => ({ path, label: 'versicolor' }),
  );
  const result = await current.request('/predict', {});
  finish({ label: 'setosa' });
  await assert.rejects(pending, (error) => error.name === 'AbortError');
  assert.deepEqual(result, { path: '/eks-iris/predict', label: 'versicolor' });
  current.close();
});

test('portfolio owner metadata is disabled and all cloud actions stay offline', async () => {
  const artifact = JSON.parse(
    await readFile(
      new URL('../src/portfolio/iris-model.json', import.meta.url),
      'utf8',
    ),
  );
  const request = createPortfolioRequest(artifact);
  const target = resolveOwnerTarget(await request('/owner-target'));
  assert.equal(target.kind, 'disabled');
  assert.equal(target.api_prefix, null);
  for (const path of ['/eks-iris', '/rehearsal-iris'])
    await assert.rejects(
      request(path + '/deploy', { replicas: 1 }),
      /connected workspace/,
    );
});
