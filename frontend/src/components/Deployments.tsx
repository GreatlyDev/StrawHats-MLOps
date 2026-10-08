import { useState } from 'react';
import type { FormEvent } from 'react';
import { FileCode, Download, Copy, Check, Layers } from 'lucide-react';
import { request } from '../api';
import type { Model, DeploymentPlan, Cluster } from '../types';
import { Badge, Empty, ErrorNotice, date } from './shared';
export default function Deployments({
  models,
  plans,
  cluster,
  refresh,
}: {
  models: Model[];
  plans: DeploymentPlan[];
  cluster: Cluster;
  refresh: () => Promise<void>;
}) {
  const [selected, setSelected] = useState('');
  const [modelId, setModelId] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [copyState, setCopyState] = useState('');
  const model = models.find((m) => m.id === modelId) || models[0];
  const plan = plans.find((p) => p.id === selected) || plans[0];
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    setBusy(true);
    setError('');
    try {
      const created = await request<DeploymentPlan>('/deployment-plans', {
        ...Object.fromEntries(form.entries()),
        model_id: model.id,
        replicas: Number(form.get('replicas')),
        cpu_millicores: Number(form.get('cpu_millicores')),
        memory_mebibytes: Number(form.get('memory_mebibytes')),
      });
      setSelected(created.id);
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function copy() {
    if (!plan) return;
    try {
      await navigator.clipboard.writeText(plan.manifest);
      setCopyState(plan.id);
    } catch {
      setError(
        'Clipboard access is unavailable. Select the manifest text or download the YAML file.',
      );
    }
  }
  function download() {
    if (!plan) return;
    const url = URL.createObjectURL(
      new Blob([plan.manifest], { type: 'text/yaml' }),
    );
    const link = document.createElement('a');
    link.href = url;
    link.download = `${plan.name}-preview.yaml`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  return (
    <>
      <div className="section-toolbar">
        <div>
          <h2>Prepare your next deployment</h2>
          <p>Generate Kubernetes manifests for review and export.</p>
        </div>
        <Badge tone="gold">
          <Layers size={13} />
          Preview workspace
        </Badge>
      </div>
      <div className="preview-notice">
        <FileCode size={22} />
        <div>
          <strong>Preview only · no cluster changes</strong>
          <p>
            Generating a manifest saves a deployment plan. It does not deploy
            workloads, scale services, or apply changes to Kubernetes.
          </p>
        </div>
      </div>
      <div className="deployment-layout">
        <section className="card">
          <div className="card-heading">
            <div>
              <h3>Manifest configuration</h3>
              <p>Define the resources for your model service.</p>
            </div>
          </div>
          {models.length ? (
            <form onSubmit={submit}>
              <fieldset className="form-section">
                <legend>
                  <span>01</span> Model & image
                </legend>
                <label className="field">
                  Registered model
                  <select
                    value={model.id}
                    onChange={(e) => setModelId(e.target.value)}
                  >
                    {models.map((m) => (
                      <option key={m.id} value={m.id}>
                        {m.name} · v{m.version}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="field">
                  Serving container image
                  <input
                    key={model.id}
                    name="image"
                    required
                    defaultValue={model.image || ''}
                    placeholder="ghcr.io/your-team/model:v1"
                  />
                  <small>
                    A runnable HTTP serving image is required, including for the
                    local example.
                  </small>
                </label>
              </fieldset>
              <fieldset className="form-section">
                <legend>
                  <span>02</span> Service identity
                </legend>
                <div className="feature-grid">
                  <label className="field">
                    Service name
                    <input
                      name="name"
                      required
                      defaultValue="model-service"
                      pattern="[a-z0-9]([a-z0-9-]*[a-z0-9])?"
                      maxLength={63}
                      title="Lowercase letters, digits and hyphens; start and end with a letter or digit."
                    />
                  </label>
                  <label className="field">
                    Namespace
                    <input
                      name="namespace"
                      required
                      defaultValue="strawhats"
                      pattern="[a-z0-9]([a-z0-9-]*[a-z0-9])?"
                      maxLength={63}
                    />
                  </label>
                </div>
              </fieldset>
              <fieldset className="form-section">
                <legend>
                  <span>03</span> Resource requests
                </legend>
                <div className="resource-grid">
                  <label className="field">
                    Replicas
                    <input
                      name="replicas"
                      type="number"
                      required
                      min="1"
                      max="10"
                      defaultValue="2"
                    />
                  </label>
                  <label className="field">
                    CPU (mCPU)
                    <input
                      name="cpu_millicores"
                      type="number"
                      required
                      min="50"
                      max="8000"
                      defaultValue="250"
                    />
                  </label>
                  <label className="field">
                    Memory (MiB)
                    <input
                      name="memory_mebibytes"
                      type="number"
                      required
                      min="128"
                      max="16384"
                      defaultValue="256"
                    />
                  </label>
                </div>
              </fieldset>
              <button className="primary full" disabled={busy}>
                <FileCode size={16} />
                {busy ? 'Generating…' : 'Generate preview'}
              </button>
            </form>
          ) : (
            <Empty title="Register a model first">
              Deployment previews require a model in your registry.
            </Empty>
          )}
          <div className="soft-note">
            <strong>
              {cluster.connected ? 'Cluster reachable' : 'Cluster unavailable'}
            </strong>
            <p>{cluster.message}</p>
          </div>
        </section>
        <section className="card manifest-card">
          <div className="card-heading">
            <div>
              <h3>Generated manifest</h3>
              <p>Review the actual saved Kubernetes YAML.</p>
            </div>
            <Badge tone="gold">Preview</Badge>
          </div>
          {plan ? (
            <>
              <label className="field">
                Saved preview
                <select
                  value={plan.id}
                  onChange={(e) => {
                    setSelected(e.target.value);
                    setCopyState('');
                  }}
                >
                  {plans.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name} · {date(p.created_at)}
                    </option>
                  ))}
                </select>
              </label>
              <div className="manifest-meta">
                <Badge>{plan.namespace}</Badge>
                <span>
                  {plan.replicas} replicas · {plan.cpu_millicores} mCPU ·{' '}
                  {plan.memory_mebibytes} MiB
                </span>
              </div>
              <div className="manifest-file">
                <FileCode size={17} aria-hidden="true" />
                <span>{plan.name}-preview.yaml</span>
                <small>YAML</small>
              </div>
              <pre tabIndex={0} aria-label="Kubernetes YAML preview">
                <code>{plan.manifest}</code>
              </pre>
              <div className="actions">
                <button className="secondary" onClick={copy}>
                  {copyState === plan.id ? (
                    <Check size={16} />
                  ) : (
                    <Copy size={16} />
                  )}
                  {copyState === plan.id ? 'Copied' : 'Copy YAML'}
                </button>
                <button className="primary" onClick={download}>
                  <Download size={16} />
                  Download YAML
                </button>
              </div>
            </>
          ) : (
            <Empty title="Your manifest will appear here">
              Generate a preview to inspect, copy, and download the YAML.
            </Empty>
          )}
        </section>
      </div>
      {error && <ErrorNotice message={error} />}
    </>
  );
}
