import type { LocalIris } from './types';

export function localIrisControls(
  status: LocalIris | null,
  stale: boolean,
  busy: boolean,
) {
  const available = Boolean(
    status?.enabled &&
    status.connected &&
    status.operator_ready &&
    status.phase !== 'Unavailable' &&
    status.phase !== 'Disabled' &&
    !stale &&
    !busy,
  );
  const verified = Boolean(
    status &&
    status.generation !== null &&
    status.generation === status.observed_generation &&
    (status.phase === 'Ready' || status.phase === 'RolledBack'),
  );
  return {
    deploy: available && status?.phase !== 'Pending',
    scale: available && verified,
    rollback: available && Boolean(status?.rollback_available),
  };
}

export function canPredictLocalIris(
  status: LocalIris | null,
  stale: boolean,
  busy: boolean,
) {
  return (
    localIrisControls(status, stale, busy).scale &&
    Boolean(status && status.ready_replicas > 0)
  );
}
