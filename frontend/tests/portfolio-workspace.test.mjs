import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const asset = JSON.parse(
  await readFile(new URL('../src/portfolio/iris-model.json', import.meta.url)),
);
const previews = JSON.parse(
  await readFile(new URL('./fixtures/preview-oracle.json', import.meta.url)),
);
async function moduleAt(name) {
  try {
    return await import(`../src/portfolio/${name}.ts`);
  } catch (error) {
    if (error.code === 'ERR_MODULE_NOT_FOUND')
      assert.fail(`Portfolio ${name} is not implemented.`);
    throw error;
  }
}
async function session() {
  return (await moduleAt('workspace')).createPortfolioRequest(asset);
}
const input = () => structuredClone(previews.cases[1].input);

test('exported preview resources match independent backend YAML at resource boundaries', async () => {
  const { validatePreview, createPreview } = await moduleAt('previews');
  for (const row of previews.cases) {
    const result = createPreview(
      validatePreview(row.input),
      'plan-test',
      '2026-10-13T12:00:00Z',
    );
    const resources = result.manifest
      .split(/^---\s*$/m)
      .filter((s) => s.trim())
      .map((s) => JSON.parse(s));
    assert.deepEqual(resources, row.resources);
    assert.equal(result.status, 'preview');
    assert.equal(result.id, 'plan-test');
  }
});

test('invalid preview identity and resources are rejected', async () => {
  const { validatePreview } = await moduleAt('previews');
  for (const patch of [
    { name: '../iris' },
    { name: 'iris\n' },
    { name: 'a'.repeat(64) },
    { name: '-iris' },
    { namespace: 'Bad' },
    { namespace: '' },
    { image: 'bad image' },
    { image: 'ab' },
    { image: 'x'.repeat(301) },
    { model_id: 'another-model' },
    { replicas: 0 },
    { replicas: null },
    { namespace: null },
    { cpu_millicores: null },
    { memory_mebibytes: null },
    { replicas: 11 },
    { replicas: 1.5 },
    { replicas: true },
    { replicas: '2' },
    { cpu_millicores: 49 },
    { cpu_millicores: 8001 },
    { cpu_millicores: NaN },
    { memory_mebibytes: 127 },
    { memory_mebibytes: 16385 },
    { memory_mebibytes: Infinity },
  ])
    assert.throws(
      () => validatePreview({ ...input(), ...patch }),
      /preview|name|namespace|image|replicas|CPU|memory|model/i,
    );
  for (const body of [null, [], 'text'])
    assert.throws(() => validatePreview(body), /preview/i);
});

test('starts with a prepared model and genuinely disconnected capabilities', async () => {
  const request = await session();
  assert.equal((await request('/models'))[0].id, 'iris-classifier');
  const overview = await request('/overview');
  assert.equal(overview.predictions, 0);
  assert.equal(overview.deployment_plans, 0);
  assert.equal(overview.median_latency_ms, null);
  assert.deepEqual(await request('/events'), []);
  assert.deepEqual((await request('/cluster')).context, null);
  assert.equal((await request('/cluster')).connected, false);
  assert.equal((await request('/health')).ai_configured, false);
});

test('real varied predictions record measured session results without network access', async (t) => {
  const request = await session();
  const calls = [];
  t.mock.method(globalThis, 'fetch', (...args) => {
    calls.push(args);
    throw new Error('Offline');
  });
  const first = await request('/models/iris-classifier/predict', {
    features: [5.1, 3.5, 1.4, 0.2],
  });
  const second = await request('/models/iris-classifier/predict', {
    features: [6.3, 3.3, 6, 2.5],
  });
  assert.equal(first.label, 'setosa');
  assert.equal(second.label, 'virginica');
  assert.equal(first.source, 'browser');
  const overview = await request('/overview');
  assert.equal(overview.predictions, 2);
  assert.deepEqual(
    overview.recent_inferences.map((n) => n.label),
    ['setosa', 'virginica'],
  );
  assert.equal(
    overview.median_latency_ms,
    (first.latency_ms + second.latency_ms) / 2,
  );
  assert.equal((await request('/events')).length, 2);
  assert.deepEqual(calls, []);
});

test('failed inputs and aborted actions create no success records', async () => {
  const request = await session();
  await assert.rejects(
    request('/models/iris-classifier/predict', { features: [0, 3, 2, 1] }),
    /measurements/i,
  );
  await assert.rejects(
    request('/deployment-plans', { ...input(), replicas: 0 }),
    /replicas/i,
  );
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(
    request('/deployment-plans', input(), controller.signal),
    { name: 'AbortError' },
  );
  assert.equal((await request('/overview')).predictions, 0);
  assert.equal((await request('/overview')).deployment_plans, 0);
  assert.deepEqual(await request('/events'), []);
});

test('refuses connected-only operations instead of fabricating success', async () => {
  const request = await session();
  for (const [path, body] of [
    ['/models/example', {}],
    ['/models', {}],
    ['/assistant', { messages: [] }],
    ['/local-iris', undefined],
    ['/local-iris/deploy', { replicas: 2 }],
    ['/local-iris/scale', { replicas: 2 }],
    ['/local-iris/rollback', {}],
    ['/local-iris/predict', { features: [5, 3, 1, 0.2] }],
    ['/unknown', undefined],
    ['/models/other/predict', { features: [5, 3, 1, 0.2] }],
  ])
    await assert.rejects(request(path, body), /connected workspace/i);
  assert.deepEqual(await request('/events'), []);
});

test('caps saved previews and records only successful preview creations', async () => {
  const request = await session();
  const first = await request('/deployment-plans', input());
  assert.equal(first.status, 'preview');
  assert.ok(first.manifest.includes('Deployment'));
  for (let n = 1; n < 50; n++) await request('/deployment-plans', input());
  assert.equal((await request('/deployment-plans')).length, 50);
  await assert.rejects(request('/deployment-plans', input()), /50|limit/i);
  assert.equal((await request('/overview')).deployment_plans, 50);
  assert.equal((await request('/events')).length, 50);
});

test('bounded histories retain accurate totals and a new session resets them', async () => {
  const request = await session();
  for (let n = 0; n < 55; n++)
    await request('/models/iris-classifier/predict', {
      features: [5.1, 3.5, 1.4, 0.2],
    });
  assert.equal((await request('/overview')).predictions, 55);
  assert.equal((await request('/overview')).recent_inferences.length, 30);
  assert.equal((await request('/events')).length, 50);
  const fresh = await session();
  assert.equal((await fresh('/overview')).predictions, 0);
  assert.deepEqual(await fresh('/events'), []);
});

test('caller mutation cannot alter retained model, event or preview records', async () => {
  const request = await session();
  const models = await request('/models');
  models[0].labels.reverse();
  models[0].name = 'tampered';
  const plan = await request('/deployment-plans', input());
  plan.manifest = 'tampered';
  const events = await request('/events');
  events[0].title = 'tampered';
  assert.equal((await request('/models'))[0].name, 'Iris classifier');
  assert.notEqual((await request('/deployment-plans'))[0].manifest, 'tampered');
  assert.notEqual((await request('/events'))[0].title, 'tampered');
  assert.equal(
    (
      await request('/models/iris-classifier/predict', {
        features: [5.1, 3.5, 1.4, 0.2],
      })
    ).label,
    'setosa',
  );
});
