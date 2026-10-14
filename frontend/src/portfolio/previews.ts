import type { DeploymentPlan } from '../types.ts';

export type PortfolioPreviewInput = Pick<
  DeploymentPlan,
  | 'model_id'
  | 'name'
  | 'namespace'
  | 'image'
  | 'replicas'
  | 'cpu_millicores'
  | 'memory_mebibytes'
>;

export function validatePreview(value: unknown): PortfolioPreviewInput {
  if (!value || typeof value !== 'object' || Array.isArray(value))
    throw new Error('Enter valid deployment preview fields.');
  const fields = value as Record<string, unknown>;
  if (fields.model_id !== 'iris-classifier')
    throw new Error('Choose the prepared Iris model for this preview.');
  const dns = (n: unknown, field: string) => {
    if (
      typeof n !== 'string' ||
      !/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(n) ||
      /[\r\n]/.test(n)
    )
      throw new Error(
        `${field} must be a lowercase DNS name of 1–63 characters.`,
      );
    return n;
  };
  const integer = (n: unknown, field: string, min: number, max: number) => {
    if (typeof n !== 'number' || !Number.isInteger(n) || n < min || n > max)
      throw new Error(`${field} must be an integer from ${min} to ${max}.`);
    return n;
  };
  const image = fields.image;
  if (
    typeof image !== 'string' ||
    image.length < 3 ||
    image.length > 300 ||
    !/^[a-zA-Z0-9][a-zA-Z0-9._:/@-]*$/.test(image) ||
    /[\r\n]/.test(image)
  )
    throw new Error('Image must be a valid 3–300 character image reference.');
  return {
    model_id: 'iris-classifier',
    name: dns(fields.name, 'Service name'),
    namespace: dns(
      fields.namespace === undefined ? 'strawhats' : fields.namespace,
      'Namespace',
    ),
    image,
    replicas: integer(
      fields.replicas === undefined ? 1 : fields.replicas,
      'Replicas',
      1,
      10,
    ),
    cpu_millicores: integer(
      fields.cpu_millicores === undefined ? 250 : fields.cpu_millicores,
      'CPU',
      50,
      8000,
    ),
    memory_mebibytes: integer(
      fields.memory_mebibytes === undefined ? 256 : fields.memory_mebibytes,
      'Memory',
      128,
      16384,
    ),
  };
}

export function createPreview(
  input: PortfolioPreviewInput,
  id: string,
  createdAt: string,
): DeploymentPlan {
  const c = validatePreview(input);
  const labels = {
    'app.kubernetes.io/name': c.name,
    'app.kubernetes.io/managed-by': 'strawhats',
  };
  const resources = [
    { apiVersion: 'v1', kind: 'Namespace', metadata: { name: c.namespace } },
    {
      apiVersion: 'apps/v1',
      kind: 'Deployment',
      metadata: { name: c.name, namespace: c.namespace, labels },
      spec: {
        replicas: c.replicas,
        revisionHistoryLimit: 3,
        selector: { matchLabels: { 'app.kubernetes.io/name': c.name } },
        template: {
          metadata: {
            labels,
            annotations: {
              'prometheus.io/scrape': 'true',
              'prometheus.io/path': '/metrics',
              'prometheus.io/port': '8000',
            },
          },
          spec: {
            containers: [
              {
                name: 'model',
                image: c.image,
                ports: [{ containerPort: 8000 }],
                resources: {
                  requests: {
                    cpu: `${c.cpu_millicores}m`,
                    memory: `${c.memory_mebibytes}Mi`,
                  },
                  limits: {
                    cpu: `${c.cpu_millicores * 2}m`,
                    memory: `${c.memory_mebibytes * 2}Mi`,
                  },
                },
                readinessProbe: {
                  httpGet: { path: '/health', port: 8000 },
                  initialDelaySeconds: 10,
                  periodSeconds: 5,
                },
                livenessProbe: {
                  httpGet: { path: '/health', port: 8000 },
                  initialDelaySeconds: 30,
                  periodSeconds: 10,
                },
                securityContext: {
                  allowPrivilegeEscalation: false,
                  capabilities: { drop: ['ALL'] },
                },
              },
            ],
            securityContext: { runAsNonRoot: true, runAsUser: 10001 },
          },
        },
      },
    },
    {
      apiVersion: 'v1',
      kind: 'Service',
      metadata: { name: c.name, namespace: c.namespace },
      spec: {
        selector: { 'app.kubernetes.io/name': c.name },
        ports: [{ port: 80, targetPort: 8000 }],
        type: 'ClusterIP',
      },
    },
  ];
  return {
    id,
    ...c,
    created_at: createdAt,
    status: 'preview',
    manifest:
      resources.map((r) => JSON.stringify(r, null, 2)).join('\n---\n') + '\n',
  };
}
