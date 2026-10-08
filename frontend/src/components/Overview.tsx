import {
  ArrowUpRight,
  Box,
  Layers,
  Timer,
  Zap,
  ArrowRight,
  Anchor,
  ScrollText,
  Compass,
  Radio,
} from 'lucide-react';
import type { Workspace } from '../types';
import { Badge, Empty, date } from './shared';
export default function Overview({
  data,
  navigate,
}: {
  data: Workspace;
  navigate: (view: 'models' | 'deployments' | 'assistant' | 'activity') => void;
}) {
  const points = data.overview.recent_inferences;
  const max = Math.max(...points.map((p) => p.latency_ms), 1);
  const coords = points
    .map(
      (p, i) =>
        `${40 + (i * 620) / Math.max(points.length - 1, 1)},${170 - (p.latency_ms / max) * 135}`,
    )
    .join(' ');
  return (
    <>
      <div className="section-intro sr-only">
        <h2>
          <Compass size={19} aria-hidden="true" /> At a glance
        </h2>
        <span>Your workspace, by the numbers</span>
      </div>
      <div className="stats">
        {[
          {
            label: 'Registered models',
            value: data.overview.models,
            icon: Box,
            note: 'Your model registry',
          },
          {
            label: 'Deployment previews',
            value: data.overview.deployment_plans,
            icon: Layers,
            note: 'Saved Kubernetes manifests',
          },
          {
            label: 'Total predictions',
            value: data.overview.predictions,
            icon: Zap,
            note: 'Measured local inference',
          },
          {
            label: 'Median latency',
            value:
              data.overview.median_latency_ms === null
                ? '—'
                : `${data.overview.median_latency_ms.toFixed(2)} ms`,
            icon: Timer,
            note: 'Last 30 recorded predictions',
          },
        ].map((stat, index) => (
          <section className={`stat card stat-${index}`} key={stat.label}>
            <div className="stat-top">
              <span className="stat-icon">
                <stat.icon size={20} aria-hidden="true" />
              </span>
              <span>{stat.label}</span>
            </div>
            <strong>{stat.value}</strong>
            <small>{stat.note}</small>
          </section>
        ))}
      </div>
      <div className="course-grid">
        <section className="welcome" aria-labelledby="voyage-title">
          <img
            className="welcome-image"
            src="/images/grand-line-voyage.webp"
            width="1200"
            height="800"
            alt=""
            fetchPriority="high"
          />
          <div className="welcome-copy">
            <span className="voyage-tag">
              <Anchor size={14} aria-hidden="true" /> THE GRAND LINE AWAITS
            </span>
            <h2 id="voyage-title">
              A clear course.
              <br />
              <em>A grand adventure.</em>
            </h2>
            <p>
              From your first model to your next deployment preview. Captain,
              you’re in control.
            </p>
            <button
              className="hero-secondary"
              onClick={() => navigate('deployments')}
            >
              Chart a deployment preview{' '}
              <ArrowRight size={17} aria-hidden="true" />
            </button>
          </div>
        </section>
        <section className="card course-card" aria-labelledby="course-title">
          <span className="eyebrow">YOUR NEXT MOVE</span>
          <h2 id="course-title">Choose your course</h2>
          {[
            {
              view: 'models' as const,
              title: 'Test a model',
              note: 'Registry & real predictions',
              icon: Box,
            },
            {
              view: 'deployments' as const,
              title: 'Prepare a preview',
              note: 'Review Kubernetes YAML',
              icon: Layers,
            },
            {
              view: 'assistant' as const,
              title: 'Ask the crew assistant',
              note: 'Guidance for your workspace',
              icon: Radio,
            },
          ].map((action) => (
            <button
              className="course-action"
              key={action.view}
              onClick={() => navigate(action.view)}
            >
              <span className="course-action-icon">
                <action.icon size={19} aria-hidden="true" />
              </span>
              <span>
                <strong>{action.title}</strong>
                <small>{action.note}</small>
              </span>
              <ArrowUpRight size={17} aria-hidden="true" />
            </button>
          ))}
        </section>
      </div>
      <div className="overview-grid">
        <section className="card chart-card">
          <div className="card-heading">
            <div>
              <span className="eyebrow">OBSERVABILITY</span>
              <h3>Inference performance</h3>
              <p>Latency from your recent predictions</p>
            </div>
            <Badge>Measured · ms</Badge>
          </div>
          {points.length ? (
            <>
              <svg
                className="chart"
                viewBox="0 0 700 210"
                role="img"
                aria-label={`Latency for ${points.length} recent inferences, from ${Math.min(...points.map((p) => p.latency_ms)).toFixed(2)} to ${Math.max(...points.map((p) => p.latency_ms)).toFixed(2)} milliseconds`}
              >
                <defs>
                  <linearGradient id="chart-fill" x1="0" y1="0" x2="0" y2="1">
                    <stop
                      offset="0%"
                      stopColor="var(--ocean)"
                      stopOpacity=".2"
                    />
                    <stop
                      offset="100%"
                      stopColor="var(--ocean)"
                      stopOpacity="0"
                    />
                  </linearGradient>
                </defs>
                {[35, 80, 125, 170].map((y) => (
                  <line
                    key={y}
                    x1="40"
                    x2="670"
                    y1={y}
                    y2={y}
                    stroke="var(--border)"
                    strokeDasharray="4 5"
                  />
                ))}
                <text x="0" y="39" className="chart-text">
                  {max.toFixed(1)}
                </text>
                <text x="10" y="174" className="chart-text">
                  0
                </text>
                <polygon
                  points={`40,170 ${coords} ${40 + ((points.length - 1) * 620) / Math.max(points.length - 1, 1)},170`}
                  fill="url(#chart-fill)"
                />
                <polyline
                  points={coords}
                  fill="none"
                  stroke="var(--ocean)"
                  strokeWidth="2.5"
                  strokeLinejoin="round"
                />
                {points.map((point, i) => (
                  <circle
                    key={`${point.created_at}-${i}`}
                    cx={40 + (i * 620) / Math.max(points.length - 1, 1)}
                    cy={170 - (point.latency_ms / max) * 135}
                    r="3.5"
                    fill="var(--ocean)"
                  >
                    <title>
                      {date(point.created_at)}: {point.latency_ms.toFixed(2)} ms
                      · {point.label}
                    </title>
                  </circle>
                ))}
                <text x="40" y="203" className="chart-text">
                  Oldest observation
                </text>
                <text x="670" y="203" textAnchor="end" className="chart-text">
                  Latest observation
                </text>
              </svg>
              <p className="chart-caption">
                {points.length} recorded{' '}
                {points.length === 1 ? 'prediction' : 'predictions'} · Oldest to
                latest
              </p>
              <details className="chart-details">
                <summary>View prediction data</summary>
                <div className="table-scroll">
                  <table>
                    <caption className="sr-only">
                      Recent measured predictions, oldest to latest
                    </caption>
                    <thead>
                      <tr>
                        <th scope="col">Recorded at</th>
                        <th scope="col">Class</th>
                        <th scope="col">Latency (ms)</th>
                      </tr>
                    </thead>
                    <tbody>
                      {points.map((point, i) => (
                        <tr key={`${point.created_at}-${i}`}>
                          <td>
                            <time dateTime={point.created_at}>
                              {date(point.created_at)}
                            </time>
                          </td>
                          <td>{point.label}</td>
                          <td>{point.latency_ms.toFixed(2)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </details>
            </>
          ) : (
            <Empty title="Your next prediction starts the story">
              Train the Iris example and run inference to see measured latency
              here.
            </Empty>
          )}
        </section>
        <section className="card readiness-card">
          <div className="card-heading">
            <div>
              <span className="eyebrow">SERVICE CHECK</span>
              <h3>Workspace readiness</h3>
              <p>Actual service status</p>
            </div>
          </div>
          <div className="readiness-row">
            <span className="status-dot green" />
            <div>
              <strong>Backend API</strong>
              <small>Connected · v{data.health.version}</small>
            </div>
            <Badge tone="green">Healthy</Badge>
          </div>
          <div className="readiness-row">
            <span
              className={`status-dot ${data.health.ai_configured ? 'green' : 'gold'}`}
            />
            <div>
              <strong>AI assistant</strong>
              <small>
                {data.health.ai_configured
                  ? data.health.ai_model
                  : 'Configure a server-side provider key'}
              </small>
            </div>
            <Badge tone={data.health.ai_configured ? 'green' : 'gold'}>
              {data.health.ai_configured ? 'Configured' : 'Unavailable'}
            </Badge>
          </div>
          <div className="readiness-row">
            <span
              className={`status-dot ${data.cluster.connected ? 'green' : 'gold'}`}
            />
            <div>
              <strong>Kubernetes</strong>
              <small>{data.cluster.context || 'No context configured'}</small>
            </div>
            <Badge tone={data.cluster.connected ? 'green' : 'gold'}>
              {data.cluster.connected ? 'Connected' : 'Offline'}
            </Badge>
          </div>
          <div className="soft-note">{data.cluster.message}</div>
          <button
            className="text-button"
            onClick={() => navigate('deployments')}
          >
            Prepare a deployment preview <ArrowRight size={15} />
          </button>
        </section>
      </div>
      <section className="card">
        <div className="card-heading">
          <div>
            <span className="eyebrow">THE SHIP’S LOG</span>
            <h3>Recent activity</h3>
            <p>The latest steps in your model lifecycle</p>
          </div>
          <button className="text-button" onClick={() => navigate('activity')}>
            View ship’s log <ArrowUpRight size={17} aria-hidden="true" />
          </button>
        </div>
        {data.events.length ? (
          data.events.slice(0, 4).map((event) => (
            <div className="event-row" key={event.id}>
              <div className="event-icon">
                <ScrollText size={18} aria-hidden="true" />
              </div>
              <div>
                <strong>{event.title}</strong>
                <p>{event.description}</p>
              </div>
              <time dateTime={event.created_at}>{date(event.created_at)}</time>
            </div>
          ))
        ) : (
          <Empty title="A fresh workspace">
            Model registrations, predictions, and saved previews will appear
            here.
          </Empty>
        )}
      </section>
    </>
  );
}
