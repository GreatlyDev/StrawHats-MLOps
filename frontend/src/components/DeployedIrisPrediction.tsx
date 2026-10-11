import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { Play } from 'lucide-react';
import { request } from '../api';
import type { DeployedPrediction } from '../types';
import { Badge, ErrorNotice, percent } from './shared';

const features = [
  'Sepal length (cm)',
  'Sepal width (cm)',
  'Petal length (cm)',
  'Petal width (cm)',
];

export default function DeployedIrisPrediction({
  available,
  busy,
  onBusyChange,
  refresh,
}: {
  available: boolean;
  busy: boolean;
  onBusyChange: (running: boolean) => void;
  refresh: () => Promise<void>;
}) {
  const [values, setValues] = useState(['5.1', '3.5', '1.4', '0.2']);
  const [result, setResult] = useState<DeployedPrediction | null>(null);
  const [error, setError] = useState('');
  const [running, setRunning] = useState(false);
  const pending = useRef<AbortController | null>(null);
  useEffect(
    () => () => {
      if (pending.current) {
        pending.current.abort();
        onBusyChange(false);
      }
    },
    [onBusyChange],
  );

  async function predict(event: FormEvent) {
    event.preventDefault();
    if (!available || pending.current) return;
    const measurements = values.map(Number);
    setError('');
    setResult(null);
    if (
      measurements.some(
        (value) => !Number.isFinite(value) || value <= 0 || value > 30,
      )
    ) {
      setError('Enter four measurements greater than 0 and at most 30 cm.');
      return;
    }
    const controller = new AbortController();
    pending.current = controller;
    setRunning(true);
    onBusyChange(true);
    try {
      const prediction = await request<DeployedPrediction>(
        '/local-iris/predict',
        { features: measurements },
        controller.signal,
      );
      if (controller.signal.aborted) return;
      setResult(prediction);
      await refresh();
    } catch (error) {
      if (!controller.signal.aborted) setError((error as Error).message);
    } finally {
      if (!controller.signal.aborted) {
        pending.current = null;
        setRunning(false);
        onBusyChange(false);
      }
    }
  }

  return (
    <div className="local-iris-prediction">
      <div className="card-heading">
        <div>
          <h3>Predict with deployed Iris</h3>
          <p>
            Send flower measurements to the Iris Service in local Kubernetes.
          </p>
        </div>
        <Badge tone="gold">Kubernetes inference</Badge>
      </div>
      <form onSubmit={predict}>
        <div className="feature-grid">
          {features.map((name, index) => (
            <label className="field" key={name}>
              {name}
              <input
                required
                type="number"
                step="any"
                min="0"
                max="30"
                disabled={busy}
                value={values[index]}
                onChange={(event) =>
                  setValues((previous) =>
                    previous.map((value, i) =>
                      i === index ? event.target.value : value,
                    ),
                  )
                }
              />
            </label>
          ))}
        </div>
        <button className="primary" disabled={!available}>
          <Play size={15} aria-hidden="true" />
          {running
            ? 'Running deployed prediction…'
            : 'Predict with deployed Iris'}
        </button>
        {!available && !busy && (
          <p className="local-iris-note">
            A current Ready or Rolled back deployment with a ready replica is
            required.
          </p>
        )}
        {error && <ErrorNotice message={error} />}
        <div aria-live="polite">
          {result && (
            <div className="prediction-result">
              <div className="card-heading">
                <div>
                  <small>KUBERNETES PREDICTED CLASS</small>
                  <h3>{result.label}</h3>
                </div>
                <Badge tone="green">
                  {result.latency_ms.toFixed(2)} ms model time
                </Badge>
              </div>
              <p className="local-iris-note">
                {result.context} / {result.namespace} / {result.service} ·
                generation {result.generation}
                <br />
                {result.image}
              </p>
              {Object.entries(result.probabilities).map(
                ([label, probability]) => (
                  <div className="probability" key={label}>
                    <div>
                      <span>{label}</span>
                      <strong>{percent(probability)}</strong>
                    </div>
                    <progress
                      max="1"
                      value={probability}
                      aria-label={`${label} probability`}
                    />
                  </div>
                ),
              )}
            </div>
          )}
        </div>
      </form>
    </div>
  );
}
