import type { WorkspaceRequest } from './portfolio/workspace.ts';

export interface OwnerTarget {
  kind: 'local' | 'eks' | 'rehearsal' | 'disabled';
  api_prefix: '/local-iris' | '/eks-iris' | '/rehearsal-iris' | null;
  title: string;
  description: string;
  badge: string;
}

const targets: Record<OwnerTarget['kind'], OwnerTarget> = {
  local: {
    kind: 'local',
    api_prefix: '/local-iris',
    title: 'Local Iris',
    description: 'Deploy and manage Iris on Docker Desktop Kubernetes.',
    badge: 'Local cluster',
  },
  eks: {
    kind: 'eks',
    api_prefix: '/eks-iris',
    title: 'AWS EKS Iris',
    description: 'Manage the verified owner release on AWS Kubernetes.',
    badge: 'AWS EKS',
  },
  rehearsal: {
    kind: 'rehearsal',
    api_prefix: '/rehearsal-iris',
    title: 'Rehearsal Iris',
    description:
      'Manage the dedicated local release rehearsal on Docker Desktop.',
    badge: 'Local rehearsal',
  },
  disabled: {
    kind: 'disabled',
    api_prefix: null,
    title: 'Cluster controls unavailable',
    description: 'A configured private owner workspace is required.',
    badge: 'Unavailable',
  },
};

export function resolveOwnerTarget(value: unknown): OwnerTarget {
  if (value && typeof value === 'object') {
    const candidate = value as Record<string, unknown>;
    for (const kind of ['local', 'eks', 'rehearsal'] as const) {
      if (
        candidate.kind === kind &&
        candidate.api_prefix === targets[kind].api_prefix
      )
        return targets[kind];
    }
  }
  return targets.disabled;
}

// A panel owns one scope. Closing it aborts transport and discards late replies,
// including replies from transports that do not honor AbortSignal themselves.
export function createOwnerRequest(
  value: unknown,
  transport: WorkspaceRequest,
) {
  const target = resolveOwnerTarget(value);
  const pending = new Set<AbortController>();
  let closed = false;
  const canceled = () =>
    new DOMException('Owner target changed.', 'AbortError');
  const request: WorkspaceRequest = async <T>(
    suffix: string,
    body?: unknown,
    signal?: AbortSignal,
  ): Promise<T> => {
    if (closed || signal?.aborted) throw canceled();
    if (!target.api_prefix)
      throw new Error('Owner cluster controls are unavailable.');
    if (!['', '/deploy', '/scale', '/rollback', '/predict'].includes(suffix))
      throw new Error('Unsupported owner operation.');
    const controller = new AbortController();
    const abort = () => controller.abort();
    signal?.addEventListener('abort', abort, { once: true });
    pending.add(controller);
    try {
      const result = await transport<T>(
        target.api_prefix + suffix,
        body,
        controller.signal,
      );
      if (closed || controller.signal.aborted) throw canceled();
      return result;
    } finally {
      signal?.removeEventListener('abort', abort);
      pending.delete(controller);
    }
  };
  return {
    request,
    close() {
      closed = true;
      for (const controller of pending) controller.abort();
      pending.clear();
    },
  };
}
