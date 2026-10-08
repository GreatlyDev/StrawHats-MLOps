import type { Workspace } from './types';
import { createRequest } from './transport';
const base = `${(import.meta.env.VITE_API_BASE_URL || '').replace(/\/$/, '')}/api`;
export const request = createRequest(base);
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
