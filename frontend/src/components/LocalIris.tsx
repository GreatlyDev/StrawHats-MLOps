import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Rocket,
  RefreshCw,
  RotateCcw,
  ArrowUpDown,
  Server,
} from 'lucide-react';
import { request } from '../api';
import { createOwnerRequest, resolveOwnerTarget } from '../owner-target';
import type { OwnerTarget } from '../owner-target';
import type { WorkspaceRequest } from '../portfolio/workspace';
import { canPredictLocalIris, localIrisControls } from '../local-iris-controls';
import type { LocalIris as IrisStatus } from '../types';
import { Badge, ErrorNotice } from './shared';
import DeployedIrisPrediction from './DeployedIrisPrediction';

type Action = 'deploy' | 'scale' | 'rollback';
const phaseLabel = {
  Disabled: 'Disabled',
  Unavailable: 'Unavailable',
  NotDeployed: 'Not deployed',
  Pending: 'Pending',
  Ready: 'Ready',
  RolledBack: 'Rolled back',
  Failed: 'Failed',
  Conflict: 'Conflict',
};

export default function LocalIris({
  isActive,
  refresh,
}: {
  isActive: boolean;
  refresh: () => Promise<void>;
}) {
  const [target, setTarget] = useState(() => resolveOwnerTarget(null));
  useEffect(() => {
    if (!isActive) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const controller = new AbortController();
    async function poll() {
      try {
        const metadata = await request<unknown>(
          '/owner-target',
          undefined,
          controller.signal,
        );
        if (!stopped) setTarget(resolveOwnerTarget(metadata));
      } catch {
        if (!stopped) setTarget(resolveOwnerTarget(null));
      } finally {
        if (!stopped) timer = setTimeout(poll, 5000);
      }
    }
    void poll();
    return () => {
      stopped = true;
      clearTimeout(timer);
      controller.abort();
    };
  }, [isActive]);
  return (
    <IrisPanel
      key={target.kind}
      target={target}
      isActive={isActive}
      refresh={refresh}
    />
  );
}

function IrisPanel({
  target,
  isActive,
  refresh,
}: {
  target: OwnerTarget;
  isActive: boolean;
  refresh: () => Promise<void>;
}) {
  const scope = useRef<ReturnType<typeof createOwnerRequest> | null>(null);
  useEffect(() => {
    const current = createOwnerRequest(target, request);
    scope.current = current;
    return () => {
      current.close();
      scope.current = null;
    };
  }, [target]);
  const ownerRequest = useCallback<WorkspaceRequest>(
    <T,>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> => {
      if (!scope.current)
        return Promise.reject(
          new DOMException('Owner target changed.', 'AbortError'),
        );
      return scope.current.request<T>(path, body, signal);
    },
    [],
  );
  const [status, setStatus] = useState<IrisStatus | null>(null);
  const [replicas, setReplicas] = useState(1);
  const [busy, setBusy] = useState<Action | 'predict' | null>(null);
  const predictionBusyChanged = useCallback(
    (running: boolean) => setBusy(running ? 'predict' : null),
    [],
  );
  const [reading, setReading] = useState(false);
  const [stale, setStale] = useState(true);
  const [readError, setReadError] = useState('');
  const [actionError, setActionError] = useState('');
  const [notice, setNotice] = useState('');
  const [reload, setReload] = useState(0);
  const actionRequest = useRef<AbortController | null>(null);

  useEffect(() => () => actionRequest.current?.abort(), []);
  useEffect(() => {
    if (!isActive) {
      setStale(true);
      setReading(false);
      return;
    }
    if (busy) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const controller = new AbortController();
    async function poll() {
      setReading(true);
      try {
        const observed = await ownerRequest<IrisStatus>(
          '',
          undefined,
          controller.signal,
        );
        if (!stopped) {
          setStatus(observed);
          setReadError('');
          setStale(false);
        }
      } catch (error) {
        if (!stopped) {
          setReadError((error as Error).message);
          setStale(true);
        }
      } finally {
        if (!stopped) {
          setReading(false);
          timer = setTimeout(poll, 5000);
        }
      }
    }
    void poll();
    return () => {
      stopped = true;
      clearTimeout(timer);
      controller.abort();
    };
  }, [isActive, busy, reload, ownerRequest]);
  useEffect(() => {
    if (status?.desired_replicas) setReplicas(status.desired_replicas);
  }, [status?.desired_replicas]);

  async function act(action: Action) {
    const controller = new AbortController();
    actionRequest.current = controller;
    setBusy(action);
    setActionError('');
    setNotice('');
    try {
      const accepted = await ownerRequest<IrisStatus>(
        `/${action}`,
        action === 'rollback' ? {} : { replicas },
        controller.signal,
      );
      if (controller.signal.aborted) return;
      setStatus(accepted);
      setStale(false);
      setReadError('');
      setNotice(
        'Request accepted. The observed status above tracks the result.',
      );
      await refresh();
    } catch (error) {
      if (!controller.signal.aborted) {
        setActionError((error as Error).message);
        setStale(true);
      }
    } finally {
      if (!controller.signal.aborted) setBusy(null);
    }
  }

  const controls = localIrisControls(status, stale, Boolean(busy));
  const verified =
    !stale && (status?.phase === 'Ready' || status?.phase === 'RolledBack');
  return (
    <section
      className="card local-iris-card"
      aria-labelledby="local-iris-title"
    >
      <div className="card-heading">
        <div>
          <h2 id="local-iris-title">{target.title}</h2>
          <p>{target.description}</p>
        </div>
        <Badge tone="gold">
          <Server size={14} aria-hidden="true" /> {target.badge}
        </Badge>
      </div>
      <div
        className="local-iris-status"
        role="status"
        aria-live="polite"
        aria-atomic="true"
      >
        <Badge tone={verified ? 'green' : 'gold'}>
          {readError
            ? 'Status unavailable'
            : stale
              ? 'Checking status…'
              : status
                ? phaseLabel[status.phase]
                : 'Checking status…'}
        </Badge>
        <span>
          {readError
            ? 'Controls are paused until status refreshes.'
            : stale
              ? 'Fetching current operator status.'
              : status?.message}
        </span>
      </div>
      <dl className="local-iris-summary">
        <div>
          <dt>Desired replicas</dt>
          <dd>{status?.desired_replicas ?? '—'}</dd>
        </div>
        <div>
          <dt>Last observed ready replicas</dt>
          <dd>{status?.generation ? status.ready_replicas : '—'}</dd>
        </div>
        <div>
          <dt>Rollback snapshot</dt>
          <dd>{status?.rollback_available ? 'Available' : 'Unavailable'}</dd>
        </div>
      </dl>
      <div className="local-iris-target">
        <span>
          {status?.context ?? 'Awaiting verified context'} /{' '}
          {status?.namespace ?? 'Awaiting verified namespace'}
        </span>
        <small>
          Target image: {status?.image ?? 'Awaiting verified status'}
        </small>
      </div>
      <div className="local-iris-actions">
        <label className="field">
          Replicas
          <select
            value={replicas}
            onChange={(event) => setReplicas(Number(event.target.value))}
            disabled={Boolean(busy) || !status?.operator_ready || stale}
          >
            <option value={1}>1 replica</option>
            <option value={2}>2 replicas</option>
          </select>
        </label>
        <div className="actions">
          <button
            type="button"
            className="primary"
            disabled={!controls.deploy}
            onClick={() => void act('deploy')}
          >
            <Rocket size={16} aria-hidden="true" />{' '}
            {busy === 'deploy' ? 'Submitting…' : 'Deploy Iris'}
          </button>
          <button
            type="button"
            className="secondary"
            disabled={!controls.scale || replicas === status?.desired_replicas}
            onClick={() => void act('scale')}
          >
            <ArrowUpDown size={16} aria-hidden="true" />{' '}
            {busy === 'scale' ? 'Submitting…' : 'Scale Iris'}
          </button>
          <button
            type="button"
            className="secondary"
            disabled={!controls.rollback}
            onClick={() => void act('rollback')}
          >
            <RotateCcw size={16} aria-hidden="true" />{' '}
            {busy === 'rollback' ? 'Submitting…' : 'Rollback Iris'}
          </button>
          <button
            type="button"
            className="secondary"
            disabled={Boolean(busy) || reading}
            onClick={() => setReload((value) => value + 1)}
          >
            <RefreshCw size={16} aria-hidden="true" /> Refresh status
          </button>
        </div>
      </div>
      <p className="local-iris-note">
        Rollback restores the most recent verified configuration. Status
        refreshes automatically while this screen is open.
      </p>
      {notice && (
        <p className="soft-note" role="status">
          {notice}
        </p>
      )}
      {readError && <ErrorNotice message={readError} />}
      {actionError && <ErrorNotice message={actionError} />}
      {isActive && (
        <DeployedIrisPrediction
          key={`${status?.generation}:${status?.image}`}
          ownerRequest={ownerRequest}
          available={canPredictLocalIris(status, stale, Boolean(busy))}
          busy={Boolean(busy)}
          onBusyChange={predictionBusyChanged}
          refresh={refresh}
        />
      )}
    </section>
  );
}
