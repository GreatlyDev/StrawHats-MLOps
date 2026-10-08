import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { Box, Plus, FlaskConical, X, Play } from 'lucide-react';
import { request } from '../api';
import type { Model, Prediction } from '../types';
import { Badge, Empty, ErrorNotice, percent } from './shared';
export default function Models({
  models,
  refresh,
}: {
  models: Model[];
  refresh: () => Promise<void>;
}) {
  const [modal, setModal] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [selected, setSelected] = useState('');
  const local = models.filter((m) => m.kind === 'local');
  const active = local.find((m) => m.id === selected) || local[0];
  async function train() {
    setBusy(true);
    setError('');
    try {
      await request('/models/example', {});
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <div className="section-toolbar">
        <div>
          <h2>Your model registry</h2>
          <p>Versioned models, measured results, and a place to start.</p>
        </div>
        <div className="actions">
          <button
            className="secondary"
            disabled={busy || models.some((m) => m.id === 'iris-classifier')}
            onClick={train}
          >
            <FlaskConical size={16} />
            {busy ? 'Training…' : 'Train Iris example'}
          </button>
          <button className="primary" onClick={() => setModal(true)}>
            <Plus size={17} />
            Register model
          </button>
        </div>
      </div>
      {error && <ErrorNotice message={error} />}
      <section className="card">
        {models.length ? (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>Model</th>
                  <th>Framework</th>
                  <th>Version</th>
                  <th>Accuracy</th>
                  <th>Type</th>
                </tr>
              </thead>
              <tbody>
                {models.map((model) => (
                  <tr key={model.id}>
                    <td>
                      <div className="model-cell">
                        <span className="model-icon">
                          <Box size={19} />
                        </span>
                        <div>
                          <strong>{model.name}</strong>
                          <small>
                            {model.description || model.image || model.id}
                          </small>
                        </div>
                      </div>
                    </td>
                    <td>{model.framework}</td>
                    <td>
                      <Badge>v{model.version}</Badge>
                    </td>
                    <td>
                      {model.metrics.accuracy === undefined
                        ? '—'
                        : percent(model.metrics.accuracy)}
                    </td>
                    <td>
                      <Badge tone={model.kind === 'local' ? 'green' : ''}>
                        {model.kind === 'local'
                          ? 'Local inference'
                          : 'Container'}
                      </Badge>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty title="Meet your first model">
            Train the reproducible Iris classifier, or register an existing
            container image.
          </Empty>
        )}
      </section>
      <div className="inference-layout">
        <section className="card">
          <div className="card-heading">
            <div>
              <h3>Try a prediction</h3>
              <p>Run the trained classifier with real feature values.</p>
            </div>
            <Play size={18} />
          </div>
          {active ? (
            <>
              <label className="field">
                Local model
                <select
                  value={active.id}
                  onChange={(e) => setSelected(e.target.value)}
                >
                  {local.map((m) => (
                    <option key={m.id} value={m.id}>
                      {m.name} · v{m.version}
                    </option>
                  ))}
                </select>
              </label>
              <PredictionForm
                key={active.id}
                model={active}
                refresh={refresh}
              />
            </>
          ) : (
            <Empty title="No local model yet">
              Train the Iris example to unlock inference. Container registration
              stores metadata only.
            </Empty>
          )}
        </section>
        <section className="card details-card">
          <span className="eyebrow">REPRODUCIBLE BY DESIGN</span>
          <h3>
            A small model.
            <br />A complete workflow.
          </h3>
          <p>
            The Iris example trains a logistic regression classifier and
            measures its performance on held-out data. Each prediction records
            probabilities and latency.
          </p>
          {active ? (
            <>
              <div className="detail-metric">
                <span>Held-out accuracy</span>
                <strong>
                  {active.metrics.accuracy === undefined
                    ? '—'
                    : percent(active.metrics.accuracy)}
                </strong>
              </div>
              <div className="detail-metric">
                <span>Macro F1 score</span>
                <strong>
                  {active.metrics.f1_macro === undefined
                    ? '—'
                    : percent(active.metrics.f1_macro)}
                </strong>
              </div>
              <div className="detail-metric">
                <span>Train / test samples</span>
                <strong>
                  {active.metrics.train_samples ?? '—'} /{' '}
                  {active.metrics.test_samples ?? '—'}
                </strong>
              </div>
            </>
          ) : (
            <div className="soft-note">
              Evaluation metrics appear after training completes.
            </div>
          )}
        </section>
      </div>
      {modal && (
        <RegisterModal close={() => setModal(false)} refresh={refresh} />
      )}
    </>
  );
}
function PredictionForm({
  model,
  refresh,
}: {
  model: Model;
  refresh: () => Promise<void>;
}) {
  const [values, setValues] = useState(['5.1', '3.5', '1.4', '0.2']);
  const [result, setResult] = useState<Prediction | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError('');
    setResult(null);
    try {
      setResult(
        await request<Prediction>(
          `/models/${encodeURIComponent(model.id)}/predict`,
          { features: values.map(Number) },
        ),
      );
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <form onSubmit={submit}>
      <div className="feature-grid">
        {model.feature_names.map((name, i) => (
          <label className="field" key={name}>
            {name}
            <input
              required
              type="number"
              step="any"
              min="0"
              value={values[i] || ''}
              onChange={(e) =>
                setValues((v) =>
                  v.map((value, j) => (j === i ? e.target.value : value)),
                )
              }
            />
          </label>
        ))}
      </div>
      <button className="primary" disabled={busy}>
        <Play size={15} />
        {busy ? 'Running prediction…' : 'Run prediction'}
      </button>
      {error && <ErrorNotice message={error} />}
      <div aria-live="polite">
        {result && (
          <div className="prediction-result">
            <div className="card-heading">
              <div>
                <small>PREDICTED CLASS</small>
                <h3>{result.label}</h3>
              </div>
              <Badge tone="green">{result.latency_ms.toFixed(2)} ms</Badge>
            </div>
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
  );
}
function RegisterModal({
  close,
  refresh,
}: {
  close: () => void;
  refresh: () => Promise<void>;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    dialog.current?.showModal();
  }, []);
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const fields = new FormData(e.currentTarget);
    setBusy(true);
    setError('');
    try {
      await request('/models', Object.fromEntries(fields.entries()));
      await refresh();
      close();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <dialog
      ref={dialog}
      className="modal"
      onCancel={(e) => {
        if (busy) e.preventDefault();
        else close();
      }}
    >
      <div className="modal-heading">
        <div>
          <span className="eyebrow">MODEL REGISTRY</span>
          <h2>Register a container model</h2>
        </div>
        <button
          type="button"
          className="icon-button"
          aria-label="Close registration"
          disabled={busy}
          onClick={close}
        >
          <X size={20} />
        </button>
      </div>
      <p>
        Save the model’s metadata and image reference for deployment previews.
      </p>
      <form onSubmit={submit}>
        <label className="field">
          Model name
          <input
            name="name"
            required
            maxLength={100}
            placeholder="Customer churn classifier"
            autoFocus
          />
        </label>
        <div className="feature-grid">
          <label className="field">
            Version
            <input name="version" required defaultValue="1.0.0" />
          </label>
          <label className="field">
            Framework
            <input name="framework" required placeholder="PyTorch" />
          </label>
        </div>
        <label className="field">
          Container image
          <input
            name="image"
            required
            placeholder="ghcr.io/your-team/model:v1"
          />
        </label>
        <label className="field">
          Description
          <textarea
            name="description"
            rows={3}
            placeholder="What does this model predict?"
          />
        </label>
        {error && <ErrorNotice message={error} />}
        <div className="modal-actions">
          <button
            type="button"
            className="secondary"
            disabled={busy}
            onClick={close}
          >
            Cancel
          </button>
          <button className="primary" disabled={busy}>
            {busy ? 'Registering…' : 'Register model'}
          </button>
        </div>
      </form>
    </dialog>
  );
}
