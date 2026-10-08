import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Compass,
  Box,
  Layers,
  Radio,
  Activity,
  RefreshCw,
  ShipWheel,
  ChevronRight,
  ArrowRight,
  Anchor,
} from 'lucide-react';
import { loadWorkspace } from './api';
import type { Workspace } from './types';
import Overview from './components/Overview';
import Models from './components/Models';
import Deployments from './components/Deployments';
import Assistant from './components/Assistant';
import { Badge, Empty, ErrorNotice, date } from './components/shared';
import { StrawHatMark } from './components/CrewArt';
type View = 'overview' | 'models' | 'deployments' | 'assistant' | 'activity';
const navigation = [
  { id: 'overview', label: 'Overview', short: 'Overview', icon: Compass },
  { id: 'models', label: 'Models', short: 'Models', icon: Box },
  {
    id: 'deployments',
    label: 'Deployment previews',
    short: 'Previews',
    icon: Layers,
  },
  { id: 'assistant', label: 'Assistant', short: 'Assistant', icon: Radio },
  { id: 'activity', label: 'Activity', short: 'Activity', icon: Activity },
] as const;

function currentView(): View {
  const requested = new URLSearchParams(window.location.search).get('view');
  return navigation.find((item) => item.id === requested)?.id ?? 'overview';
}

function viewLink(view: View) {
  const url = new URL(window.location.href);
  if (view === 'overview') url.searchParams.delete('view');
  else url.searchParams.set('view', view);
  return `${url.pathname}${url.search}`;
}

export default function App() {
  const [view, setView] = useState<View>(currentView);
  const firstView = useRef(true);
  const [data, setData] = useState<Workspace | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const navigate = useCallback(
    (next: View) => {
      if (next === view) return;
      window.history.pushState(null, '', viewLink(next));
      setView(next);
    },
    [view],
  );
  useEffect(() => {
    const onBack = () => setView(currentView());
    window.addEventListener('popstate', onBack);
    return () => window.removeEventListener('popstate', onBack);
  }, []);
  useEffect(() => {
    if (firstView.current) {
      firstView.current = false;
      return;
    }
    document.getElementById('main')?.focus({ preventScroll: true });
    window.scrollTo(0, 0);
  }, [view]);
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
          href={viewLink('overview')}
          onClick={(e) => {
            if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
            e.preventDefault();
            navigate('overview');
          }}
        >
          <div className="brand-icon">
            <StrawHatMark />
          </div>
          <div>
            <strong>StrawHats</strong>
            <small>MLOPS COMMAND DECK</small>
          </div>
        </a>
        <span className="nav-label">YOUR WORKSPACE</span>
        <nav aria-label="Main navigation">
          {navigation.map((item) => (
            <a
              key={item.id}
              href={viewLink(item.id)}
              className={`nav-item ${view === item.id ? 'active' : ''}`}
              aria-label={item.label}
              aria-current={view === item.id ? 'page' : undefined}
              onClick={(e) => {
                if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
                e.preventDefault();
                navigate(item.id);
              }}
            >
              <item.icon size={20} aria-hidden="true" />
              <span className="nav-full-label">{item.label}</span>
              <span className="nav-short-label" aria-hidden="true">
                {item.short}
              </span>
              {item.id === 'models' && data && (
                <span className="nav-count" aria-hidden="true">
                  {data.models.length}
                </span>
              )}
              {view === item.id && (
                <ChevronRight size={15} aria-hidden="true" />
              )}
            </a>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="voyage-card">
            <span className="voyage-icon">
              <Anchor size={24} aria-hidden="true" />
            </span>
            <small>A COURSE OF YOUR OWN</small>
            <strong>Build. Test. Set sail.</strong>
            <p>
              Your models, your decisions. Every deployment starts with a
              preview.
            </p>
          </div>
          <div className="workspace-identity">
            <span>
              <StrawHatMark />
            </span>
            <div>
              <strong>The Straw Hats</strong>
              <small>Group 12 · COSC 472</small>
            </div>
          </div>
        </div>
      </aside>
      <div className="main-shell">
        <header className="topbar">
          <div className="breadcrumb">
            <ShipWheel size={18} aria-hidden="true" /> Command deck{' '}
            <ChevronRight size={14} aria-hidden="true" />
            <strong>{title}</strong>
          </div>
          <div className="topbar-right">
            <span role="status" aria-live="polite" aria-atomic="true">
              <Badge tone={data && !error ? 'green' : 'gold'}>
                <span
                  className={`status-dot ${data && !error ? 'green' : 'gold'}`}
                />
                {loading && !data
                  ? 'Connecting to API'
                  : data && !error
                    ? 'API connected'
                    : 'API unavailable'}
              </Badge>
            </span>
            <button
              className="icon-button refresh-button"
              aria-label="Refresh workspace"
              disabled={loading}
              onClick={() => void refresh()}
            >
              <RefreshCw size={18} className={loading ? 'spinning' : ''} />
            </button>
            <div className="avatar" aria-label="StrawHats team">
              <StrawHatMark />
            </div>
          </div>
        </header>
        <main id="main" tabIndex={-1}>
          <div className="page-heading">
            <div>
              <span className="eyebrow">STRAWHATS / GROUP 12</span>
              <h1>{title}</h1>
              {view === 'overview' && (
                <p>Your crew’s model operations, at a glance.</p>
              )}
            </div>
            {view === 'overview' ? (
              <button className="primary" onClick={() => navigate('models')}>
                Open model registry <ArrowRight size={18} aria-hidden="true" />
              </button>
            ) : (
              <span className="workspace-mode">Development workspace</span>
            )}
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
                <Overview data={data} navigate={navigate} />
              )}
              <div hidden={view !== 'models'}>
                <Models
                  models={data.models}
                  refresh={refresh}
                  isActive={view === 'models'}
                />
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
                      <h2>The ship’s log</h2>
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
            <span className="footer-brand">
              <ShipWheel size={16} aria-hidden="true" /> StrawHats MLOps · Group
              12
            </span>
            <span>A grand adventure, one model at a time.</span>
          </footer>
        </main>
      </div>
    </div>
  );
}
