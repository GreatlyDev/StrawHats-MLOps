import type { Prediction } from '../types.ts';

export interface IrisModelArtifact {
  schema_version: 1;
  id: string;
  name: string;
  version: string;
  feature_names: string[];
  labels: string[];
  classes: number[];
  scaler_mean: number[];
  scaler_scale: number[];
  coefficients: number[][];
  intercepts: number[];
  metrics: Record<string, number>;
  training_split: {
    train_samples: number;
    test_samples: number;
    random_state: number;
    stratified: boolean;
  };
  provenance: Record<string, string>;
  content_sha256: string;
}

export interface BrowserPrediction extends Prediction {
  source: 'browser';
}

function vector(value: unknown, length: number): value is number[] {
  return (
    Array.isArray(value) &&
    value.length === length &&
    value.every((n) => typeof n === 'number' && Number.isFinite(n))
  );
}

export function validateIrisArtifact(value: unknown): IrisModelArtifact {
  const a = value as IrisModelArtifact | null;
  const labels = ['setosa', 'versicolor', 'virginica'];
  const features = [
    'sepal length (cm)',
    'sepal width (cm)',
    'petal length (cm)',
    'petal width (cm)',
  ];
  if (
    !a ||
    a.schema_version !== 1 ||
    a.id !== 'iris-classifier' ||
    typeof a.name !== 'string' ||
    !a.name ||
    typeof a.version !== 'string' ||
    !a.version ||
    !Array.isArray(a.labels) ||
    a.labels.length !== 3 ||
    a.labels.some((label, i) => label !== labels[i]) ||
    !Array.isArray(a.feature_names) ||
    a.feature_names.length !== 4 ||
    a.feature_names.some((name, i) => name !== features[i]) ||
    !vector(a.classes, 3) ||
    a.classes.some((n, i) => n !== i) ||
    !vector(a.scaler_mean, 4) ||
    !vector(a.scaler_scale, 4) ||
    a.scaler_scale.some((n) => n <= 0) ||
    !vector(a.intercepts, 3) ||
    !Array.isArray(a.coefficients) ||
    a.coefficients.length !== 3 ||
    a.coefficients.some((row) => !vector(row, 4)) ||
    !a.metrics ||
    ['accuracy', 'f1_macro', 'train_accuracy'].some(
      (key) =>
        !Number.isFinite(a.metrics[key]) ||
        a.metrics[key] < 0 ||
        a.metrics[key] > 1,
    ) ||
    a.metrics.train_samples !== 120 ||
    a.metrics.test_samples !== 30 ||
    a.training_split?.train_samples !== 120 ||
    a.training_split?.test_samples !== 30 ||
    a.training_split?.random_state !== 42 ||
    a.training_split?.stratified !== true ||
    !a.provenance ||
    ['algorithm', 'scikit_learn', 'model_source_sha256', 'dataset_source'].some(
      (key) => typeof a.provenance[key] !== 'string' || !a.provenance[key],
    ) ||
    typeof a.content_sha256 !== 'string' ||
    !/^[a-f0-9]{64}$/.test(a.content_sha256)
  ) {
    throw new Error(
      'The prepared Iris model artifact is invalid. Rebuild its verified export.',
    );
  }
  return structuredClone(a);
}

export function predictBrowserIris(
  artifact: IrisModelArtifact,
  features: unknown,
): BrowserPrediction {
  if (!vector(features, 4) || features.some((n) => n <= 0 || n > 30)) {
    throw new Error(
      'Enter exactly four finite measurements greater than 0 and at most 30 cm.',
    );
  }
  const started = performance.now();
  const normalized = features.map(
    (n, i) => (n - artifact.scaler_mean[i]) / artifact.scaler_scale[i],
  );
  const logits = artifact.coefficients.map((row, i) =>
    row.reduce((sum, n, j) => sum + n * normalized[j], artifact.intercepts[i]),
  );
  if (logits.some((n) => !Number.isFinite(n)))
    throw new Error('The model artifact produced invalid numeric scores.');
  const maximum = Math.max(...logits);
  const weights = logits.map((n) => Math.exp(n - maximum));
  const total = weights.reduce((sum, n) => sum + n, 0);
  const values = weights.map((n) => n / total);
  const index = logits.indexOf(maximum);
  return {
    source: 'browser',
    label: artifact.labels[index],
    confidence: values[index],
    probabilities: Object.fromEntries(
      artifact.labels.map((label, i) => [label, values[i]]),
    ),
    latency_ms: Math.max(performance.now() - started, 0.001),
  };
}
