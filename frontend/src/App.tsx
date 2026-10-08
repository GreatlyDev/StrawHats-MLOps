import { useCallback, useEffect, useState } from 'react';
import {
  LayoutDashboard,
  Box,
  Layers,
  Sparkles,
  Activity,
  RefreshCw,
  Ship,
  ChevronRight,
  ArrowUpRight,
} from 'lucide-react';
import { loadWorkspace } from './api';
import type { Workspace } from './types';
import Overview from './components/Overview';
import Models from './components/Models';
import Deployments from './components/Deployments';
import Assistant from './components/Assistant';
import { Badge, Empty, ErrorNotice, date } from './components/shared';
type View = 'overview' | 'models' | 'deployments' | 'assistant' | 'activity';
const navigation = [
  { id: 'overview', label: 'Overview', icon: LayoutDashboard },
  { id: 'models', label: 'Models', icon: Box },
  { id: 'deployments', label: 'Deployment previews', icon: Layers },
  { id: 'assistant', label: 'Assistant', icon: Sparkles },
  { id: 'activity', label: 'Activity', icon: Activity },
] as const;
export default function App() {
  const [view, setView] = useState<View>('overview');
  const [data, setData] = useState<Workspace | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const refresh = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setError('');
    try {
      const result = await loadWorkspace(signal);
      if (!signal?.aborted) setData(result);
    } catch (e) {
      if (!signal?.aborted) setError((e as Error).message);
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    void refresh(controller.signal);
    return () => controller.abort();
  }, [refresh]);
  const title = navigation.find((item) => item.id === view)!.label;
  return (
    <div className="app-shell">
      <a className="skip-link" href="#main">
        Skip to main content
      </a>
      <aside className="sidebar">
        <a
          className="brand"
          href="#"
          onClick={(e) => {
            e.preventDefault();
            setView('overview');
          }}
        >
          <div className="brand-icon">
            <Ship size={25} />
          </div>
          <div>
            <strong>
              StrawHats<span>.</span>
            </strong>
            <small>MLOPS WORKSPACE</small>
          </div>
        </a>
        <span className="nav-label">WORKSPACE</span>
        <nav aria-label="Main navigation">
          {navigation.map((item) => (
            <button
              key={item.id}
              className={`nav-item ${view === item.id ? 'active' : ''}`}
              aria-current={view === item.id ? 'page' : undefined}
              onClick={() => setView(item.id)}
            >
              <item.icon size={19} />
              <span>{item.label}</span>
              {view === item.id && <ChevronRight size={15} />}
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="voyage-card">
            <span className="voyage-icon">
              <Layers size={20} />
            </span>
            <strong>Build. Measure. Set sail.</strong>
            <p>
              Start with a model.
              <br />
              Prepare the next step.
            </p>
            <button onClick={() => setView('models')}>
              Open registry <ArrowUpRight size={15} />
            </button>
          </div>
          <div className="workspace-identity">
            <span>SH</span>
            <div>
              <strong>StrawHats team</strong>
              <small>Local development</small>
            </div>
            <span className="status-dot gold" />
          </div>
        </div>
      </aside>
      <div className="main-shell">
        <header className="topbar">
          <div className="breadcrumb">
            Workspace <ChevronRight size={14} />
            <strong>{title}</strong>
          </div>
          <div className="topbar-right">
            <Badge tone={data && !error ? 'green' : 'gold'}>
              <span
                className={`status-dot ${data && !error ? 'green' : 'gold'}`}
              />
              {data && !error ? 'API connected' : 'API unavailable'}
            </Badge>
            <button
              className="icon-button refresh-button"
              aria-label="Refresh workspace"
              disabled={loading}
              onClick={() => void refresh()}
            >
              <RefreshCw size={18} className={loading ? 'spinning' : ''} />
            </button>
            <div className="avatar" aria-label="StrawHats team">
              SH
            </div>
          </div>
        </header>
        <main id="main" tabIndex={-1}>
          <div className="page-heading">
            <div>
              <span className="eyebrow">STRAWHATS / MLOPS</span>
              <h1>{title}</h1>
            </div>
            <span className="workspace-mode">Development workspace</span>
          </div>
          {error && (
            <div className="connection-error">
              <ErrorNotice message={error} />
              <button
                className="secondary"
                disabled={loading}
                onClick={() => void refresh()}
              >
                Retry connection
              </button>
            </div>
          )}
          {loading && !data ? (
            <div className="loading-state" role="status">
              <RefreshCw size={24} className="spinning" />
              <h3>Connecting to your workspace</h3>
              <p>Loading model records and service status…</p>
            </div>
          ) : data ? (
            <>
              {view === 'overview' && (
                <Overview data={data} navigate={setView} />
              )}
              <div hidden={view !== 'models'}>
                <Models models={data.models} refresh={refresh} />
              </div>
              <div hidden={view !== 'deployments'}>
                <Deployments
                  models={data.models}
                  plans={data.plans}
                  cluster={data.cluster}
                  refresh={refresh}
                />
              </div>
              <div hidden={view !== 'assistant'}>
                <Assistant health={data.health} />
              </div>
              {view === 'activity' && (
                <>
                  <div className="section-toolbar">
                    <div>
                      <h2>Your workspace timeline</h2>
                      <p>Real events recorded by the backend.</p>
                    </div>
                    <Badge>{data.events.length} events</Badge>
                  </div>
                  <section className="card">
                    {data.events.length ? (
                      data.events.map((event) => (
                        <div className="event-row" key={event.id}>
                          <div className="event-icon">
                            <Activity size={18} />
                          </div>
                          <div>
                            <strong>{event.title}</strong>
                            <p>{event.description}</p>
                            <Badge>{event.kind}</Badge>
                          </div>
                          <time dateTime={event.created_at}>
                            {date(event.created_at)}
                          </time>
                        </div>
                      ))
                    ) : (
                      <Empty title="No activity recorded">
                        Train a model, run a prediction, or generate a
                        deployment preview to get started.
                      </Empty>
                    )}
                  </section>
                </>
              )}
            </>
          ) : (
            <Empty title="Your backend is not connected">
              Start the API and retry to load your workspace.
            </Empty>
          )}
          <footer>
            StrawHats MLOps <span>Model operations with a clear course.</span>
          </footer>
        </main>
      </div>
    </div>
  );
}
