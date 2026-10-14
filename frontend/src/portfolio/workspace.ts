import type {
  DeploymentPlan,
  Event,
  Inference,
  Model,
  Overview,
} from '../types.ts';
import { predictBrowserIris, validateIrisArtifact } from './inference.ts';
import { createPreview, validatePreview } from './previews.ts';

export type WorkspaceRequest = <T>(
  path: string,
  body?: unknown,
  signal?: AbortSignal,
) => Promise<T>;

export function createPortfolioRequest(asset: unknown): WorkspaceRequest {
  const artifact = validateIrisArtifact(asset);
  const model: Model = {
    id: artifact.id,
    name: artifact.name,
    version: artifact.version,
    framework: 'scikit-learn',
    kind: 'local',
    image: null,
    description:
      'Prepared logistic regression model. Predictions are calculated in your browser.',
    created_at: new Date().toISOString(),
    metrics: artifact.metrics,
    feature_names: artifact.feature_names,
    labels: artifact.labels,
  };
  const plans: DeploymentPlan[] = [];
  const inferences: Inference[] = [];
  const events: Event[] = [];
  let predictions = 0;
  let eventId = 0;
  function record(
    kind: string,
    title: string,
    description: string,
    createdAt: string,
  ) {
    events.unshift({
      id: ++eventId,
      kind,
      title,
      description,
      created_at: createdAt,
    });
    if (events.length > 50) events.pop();
  }
  function overview(): Overview {
    const times = inferences.map((n) => n.latency_ms).sort((a, b) => a - b);
    const middle = Math.floor(times.length / 2);
    return {
      models: 1,
      deployment_plans: plans.length,
      predictions,
      median_latency_ms: times.length
        ? times.length % 2
          ? times[middle]
          : (times[middle - 1] + times[middle]) / 2
        : null,
      recent_inferences: inferences,
    };
  }
  return async function request<T>(
    path: string,
    body?: unknown,
    signal?: AbortSignal,
  ): Promise<T> {
    if (signal?.aborted)
      throw new DOMException('Request canceled.', 'AbortError');
    let result: unknown;
    if (body === undefined) {
      switch (path) {
        case '/models':
          result = [model];
          break;
        case '/overview':
          result = overview();
          break;
        case '/deployment-plans':
          result = plans;
          break;
        case '/events':
          result = events;
          break;
        case '/cluster':
          result = {
            connected: false,
            context: null,
            message:
              'Portfolio mode has no connected cluster. Configuration previews remain available.',
          };
          break;
        case '/health':
          result = {
            status: 'browser',
            ai_configured: false,
            ai_model: 'Unavailable in portfolio mode',
            version: '0.1.0',
          };
          break;
        default:
          throw new Error('This operation requires the connected workspace.');
      }
    } else if (path === '/models/iris-classifier/predict') {
      const data =
        body && typeof body === 'object' && !Array.isArray(body)
          ? (body as Record<string, unknown>)
          : {};
      const prediction = predictBrowserIris(artifact, data.features);
      const createdAt = new Date().toISOString();
      predictions++;
      inferences.push({
        model_id: model.id,
        label: prediction.label,
        latency_ms: prediction.latency_ms,
        created_at: createdAt,
      });
      if (inferences.length > 30) inferences.shift();
      record(
        'inference',
        'Browser prediction completed',
        `${prediction.label} · ${prediction.latency_ms.toFixed(2)} ms in this session`,
        createdAt,
      );
      result = prediction;
    } else if (path === '/deployment-plans') {
      const input = validatePreview(body);
      if (plans.length >= 50)
        throw new Error(
          'This browser session has reached its 50-preview limit. Reload to start a new session.',
        );
      const createdAt = new Date().toISOString();
      const plan = createPreview(
        input,
        `plan-${crypto.randomUUID()}`,
        createdAt,
      );
      plans.unshift(plan);
      record(
        'deployment',
        'Browser preview created',
        `${plan.name} · export only; no cluster changes`,
        createdAt,
      );
      result = plan;
    } else {
      throw new Error('This operation requires the connected workspace.');
    }
    return structuredClone(result) as T;
  };
}
