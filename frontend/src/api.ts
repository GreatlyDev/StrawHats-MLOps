import type { Workspace } from './types';
import { createRequest } from './transport';
import { resolveRuntime, selectRequest } from './runtime';
import { createPortfolioRequest } from './portfolio/workspace';
import iris from './portfolio/iris-model.json';
export const runtime = resolveRuntime(
  import.meta.env.VITE_APP_MODE,
  import.meta.env.VITE_API_BASE_URL,
);
const http = createRequest(`${runtime.apiBase.replace(/\/$/, '')}/api`);
export const request = selectRequest(
  runtime,
  http,
  runtime.mode === 'portfolio' ? createPortfolioRequest(iris) : http,
);
export async function loadWorkspace(signal?: AbortSignal): Promise<Workspace> {
  const [overview, models, plans, events, cluster, health] = await Promise.all([
    request<Workspace['overview']>('/overview', undefined, signal),
    request<Workspace['models']>('/models', undefined, signal),
    request<Workspace['plans']>('/deployment-plans', undefined, signal),
    request<Workspace['events']>('/events', undefined, signal),
    request<Workspace['cluster']>('/cluster', undefined, signal),
    request<Workspace['health']>('/health', undefined, signal),
  ]);
  return { overview, models, plans, events, cluster, health };
}
