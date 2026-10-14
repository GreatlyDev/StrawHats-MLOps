import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const asset = JSON.parse(
  await readFile(new URL('../src/portfolio/iris-model.json', import.meta.url)),
);
const oracle = JSON.parse(
  await readFile(new URL('./fixtures/iris-oracle.json', import.meta.url)),
);
async function evaluator() {
  try {
    return await import('../src/portfolio/inference.ts');
  } catch (error) {
    if (error.code === 'ERR_MODULE_NOT_FOUND')
      assert.fail('Browser inference is not implemented.');
    throw error;
  }
}

test('matches independent Python probabilities for every dataset and boundary vector', async () => {
  const { validateIrisArtifact, predictBrowserIris } = await evaluator();
  const model = validateIrisArtifact(asset);
  for (const row of oracle.cases) {
    const result = predictBrowserIris(model, row.features);
    assert.equal(result.label, row.label, JSON.stringify(row.features));
    assert.equal(result.source, 'browser');
    assert.ok(Number.isFinite(result.latency_ms) && result.latency_ms > 0);
    let sum = 0;
    for (const [i, label] of model.labels.entries()) {
      const probability = result.probabilities[label];
      assert.ok(
        Number.isFinite(probability) && probability >= 0 && probability <= 1,
      );
      assert.ok(
        Math.abs(probability - row.probabilities[i]) <= 1e-10,
        `${label}: ${JSON.stringify(row.features)}`,
      );
      sum += probability;
    }
    assert.ok(Math.abs(sum - 1) <= 1e-12);
    assert.equal(result.confidence, result.probabilities[result.label]);
  }
});

test('rejects invalid measurements without returning a prediction', async () => {
  const { validateIrisArtifact, predictBrowserIris } = await evaluator();
  const model = validateIrisArtifact(asset);
  for (const features of [
    null,
    {},
    [],
    [1, 2, 3],
    [1, 2, 3, 4, 5],
    ['1', 2, 3, 4],
    [0, 2, 3, 4],
    [-1, 2, 3, 4],
    [30.1, 2, 3, 4],
    [NaN, 2, 3, 4],
    [Infinity, 2, 3, 4],
    [true, 2, 3, 4],
  ]) {
    assert.throws(
      () => predictBrowserIris(model, features),
      /four|measurements|finite|30/i,
    );
  }
});

test('rejects unusable model tensors and misordered class metadata', async () => {
  const { validateIrisArtifact } = await evaluator();
  for (const mutate of [
    (a) => a.coefficients.pop(),
    (a) => a.coefficients[0].pop(),
    (a) => a.intercepts.pop(),
    (a) => a.scaler_mean.pop(),
    (a) => (a.scaler_scale[0] = 0),
    (a) => (a.scaler_scale[0] = -1),
    (a) => (a.coefficients[1][1] = NaN),
    (a) => (a.intercepts[0] = Infinity),
    (a) => (a.classes = [2, 1, 0]),
    (a) => (a.labels = ['setosa', 'setosa', 'virginica']),
    (a) => a.labels.reverse(),
    (a) => a.feature_names.reverse(),
    (a) => (a.schema_version = 2),
    (a) => (a.metrics.accuracy = NaN),
  ]) {
    const corrupt = structuredClone(asset);
    mutate(corrupt);
    assert.throws(() => validateIrisArtifact(corrupt), /model|artifact/i);
  }
  assert.throws(() => validateIrisArtifact(null), /model|artifact/i);
});

test('does not change the model or caller measurements during inference', async () => {
  const { validateIrisArtifact, predictBrowserIris } = await evaluator();
  const original = JSON.stringify(asset);
  const measurements = [6.3, 3.3, 6, 2.5];
  predictBrowserIris(validateIrisArtifact(asset), measurements);
  assert.equal(JSON.stringify(asset), original);
  assert.deepEqual(measurements, [6.3, 3.3, 6, 2.5]);
});

test('rejects sparse arrays with missing model values or measurements', async () => {
  const { validateIrisArtifact, predictBrowserIris } = await evaluator();
  const model = validateIrisArtifact(asset);
  for (const field of [
    'labels',
    'feature_names',
    'classes',
    'scaler_mean',
    'scaler_scale',
    'intercepts',
    'coefficients',
  ]) {
    const corrupt = structuredClone(asset);
    corrupt[field] = new Array(corrupt[field].length);
    assert.throws(
      () => validateIrisArtifact(corrupt),
      /model|artifact/i,
      field,
    );
  }
  const corrupt = structuredClone(asset);
  corrupt.coefficients[1] = new Array(4);
  assert.throws(() => validateIrisArtifact(corrupt), /model|artifact/i);
  assert.throws(() => predictBrowserIris(model, new Array(4)), /measurements/i);
});
