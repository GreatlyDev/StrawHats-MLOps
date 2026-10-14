import type { WorkspaceRequest } from './portfolio/workspace.ts';

export interface Runtime {
  mode: 'workspace' | 'portfolio';
  apiBase: string;
}

export function resolveRuntime(mode?: string, apiBase = ''): Runtime {
  const selected = mode || 'workspace';
  if (selected !== 'workspace' && selected !== 'portfolio')
    throw new Error('VITE_APP_MODE must be workspace or portfolio.');
  if (selected === 'portfolio' && apiBase !== '')
    throw new Error(
      'Portfolio mode requires VITE_API_BASE_URL to be unset or empty.',
    );
  return { mode: selected, apiBase };
}

export function selectRequest(
  runtime: Runtime,
  http: WorkspaceRequest,
  portfolio: WorkspaceRequest,
): WorkspaceRequest {
  return runtime.mode === 'portfolio' ? portfolio : http;
}
