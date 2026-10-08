export interface Model {
  id: string;
  name: string;
  version: string;
  framework: string;
  description: string;
  image: string | null;
  kind: 'local' | 'container';
  created_at: string;
  metrics: Record<string, number>;
  feature_names: string[];
  labels: string[];
}
export interface Inference {
  created_at: string;
  latency_ms: number;
  label: string;
  model_id: string;
}
export interface Overview {
  models: number;
  deployment_plans: number;
  predictions: number;
  median_latency_ms: number | null;
  recent_inferences: Inference[];
}
export interface DeploymentPlan {
  id: string;
  model_id: string;
  name: string;
  namespace: string;
  image: string;
  replicas: number;
  cpu_millicores: number;
  memory_mebibytes: number;
  created_at: string;
  status: 'preview';
  manifest: string;
}
export interface Event {
  id: number;
  kind: string;
  title: string;
  description: string;
  created_at: string;
}
export interface Cluster {
  connected: boolean;
  context: string | null;
  message: string;
}
export interface Health {
  status: string;
  ai_configured: boolean;
  ai_model: string;
  version: string;
}
export interface Prediction {
  label: string;
  confidence: number;
  probabilities: Record<string, number>;
  latency_ms: number;
}
export interface Message {
  role: 'user' | 'assistant';
  content: string;
}
export interface Workspace {
  overview: Overview;
  models: Model[];
  plans: DeploymentPlan[];
  events: Event[];
  cluster: Cluster;
  health: Health;
}
