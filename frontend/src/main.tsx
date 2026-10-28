import { StrictMode, useCallback, useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { marked } from 'marked';
import type { Adapter, CanonicalEvent, Finding, Overview, Report, Session, SessionDetail } from './types';
import { PromptCard, ToolCard } from './PromptCard';
import './style.css';

type Page = 'Overview' | 'Sessions' | 'Findings' | 'Reports' | 'Coverage';

const names: Record<string, string> = {
  antigravity: 'Antigravity',
  claude_code: 'Claude Code',
  codex: 'OpenAI Codex',
  gemini_cli: 'Gemini CLI',
  generic: 'Generic',
};

const short: Record<string, string> = {
  antigravity: 'Ag',
  claude_code: 'Cl',
  codex: 'Cx',
  gemini_cli: 'Ge',
  generic: '·',
};

const number = (v: number | null | undefined) =>
  v == null
    ? '—'
    : new Intl.NumberFormat('en', {
        notation: v > 9999 ? 'compact' : 'standard',
        maximumFractionDigits: 1,
      }).format(v);

const money = (v: number | null | undefined) =>
  v == null
    ? '—'
    : new Intl.NumberFormat('en', {
        style: 'currency',
        currency: 'USD',
        maximumFractionDigits: 3,
      }).format(v);

const date = (v: string | null) =>
  !v || v.startsWith('1970')
    ? 'Time unavailable'
    : new Date(v).toLocaleString(undefined, {
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
      });

async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, options);
  if (!response.ok) {
    throw new Error(`Request failed (${response.status}). Check that the local daemon is running.`);
  }
  return response.json() as Promise<T>;
}

function Mark({ agent }: { agent: string }) {
  return <span className={`agent-mark ${agent}`}>{short[agent] || agent.slice(0, 2)}</span>;
}

function Status({ value }: { value: string }) {
  return (
    <span className={`status ${value}`}>
      <i />
      {value}
    </span>
  );
}

function Empty({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="empty">
      <span className="empty-icon">⌁</span>
      <h3>{title}</h3>
      <div>{children}</div>
    </div>
  );
}

function FindingCard({ finding, openSession }: { finding: Finding; openSession: (id: string) => Promise<void> }) {
  const ev = (() => {
    try {
      return JSON.parse(finding.evidence) as Record<string, unknown>;
    } catch {
      return {};
    }
  })();

  const isContextGrowth = finding.title.includes('Context grew');
  const isFailureLoop = finding.title.includes('Repeated tool failures');
  const isSlowTool = finding.title.includes('Tool took over');
  const isCacheOpportunity = finding.detector === 'cache_opportunity';

  const minIn = typeof ev.min_input === 'number' ? ev.min_input : null;
  const maxIn = typeof ev.max_input === 'number' ? ev.max_input : null;
  const growthPct = minIn && maxIn ? Math.round(((maxIn - minIn) / Math.max(1, minIn)) * 100) : null;

  const rootCause = typeof ev.root_cause === 'string' ? ev.root_cause : null;
  const idleFormatted = typeof ev.idle_formatted === 'string' ? ev.idle_formatted : null;
  const elapsedFormatted = typeof ev.elapsed_formatted === 'string' ? ev.elapsed_formatted : null;
  const callSeq = typeof ev.call_sequence === 'number' ? ev.call_sequence : null;
  const uncachedTokens = typeof ev.input_uncached === 'number' ? ev.input_uncached : null;
  const prevCacheRead = typeof ev.previous_cache_read === 'number' ? ev.previous_cache_read : null;

  return (
    <article className="panel finding" key={finding.id}>
      <div className="finding-icon">✳</div>
      <div className="finding-main">
        <div className="finding-meta">
          <Status value={finding.severity} />
          <span className="finding-agent-badge">
            <Mark agent={finding.agent} /> {names[finding.agent] || finding.agent}
          </span>
          <span className="finding-time">{date(finding.ts)}</span>
        </div>

        <h2>{finding.title}</h2>

        {isContextGrowth && minIn != null && maxIn != null && (
          <div className="finding-metric-badge warning">
            <span className="metric-tag">📈 Context Surge</span>
            <strong>{number(minIn)} tokens</strong>
            <span className="arrow">➔</span>
            <strong>{number(maxIn)} tokens</strong>
            {growthPct != null && <span className="growth-tag">+{growthPct}%</span>}
            <span className="call-note">across {String(ev.call_count || 'multiple')} model calls</span>
          </div>
        )}

        {isFailureLoop && (
          <div className="finding-metric-badge error">
            <span className="metric-tag">🔁 Loop Detected</span>
            <strong>{String(ev.count || 3)} consecutive failures</strong>
            <span className="call-note">with identical tool input</span>
          </div>
        )}

        {isSlowTool && typeof ev.duration_ms === 'number' && (
          <div className="finding-metric-badge warning">
            <span className="metric-tag">⏱ Execution Delay</span>
            <strong>{(ev.duration_ms / 1000).toFixed(1)} seconds</strong>
            <span className="call-note">(threshold: 120s)</span>
          </div>
        )}

        {isCacheOpportunity && (
          <div className={`finding-metric-badge ${rootCause === 'session_warmup' ? 'info' : 'warning'}`}>
            {rootCause === 'cache_ttl_expired' && (
              <>
                <span className="metric-tag">⏳ Cache TTL Expired</span>
                <strong>{uncachedTokens != null ? number(uncachedTokens) : '—'} tokens uncached</strong>
                <span className="call-note">after {idleFormatted || 'idle'} pause</span>
              </>
            )}
            {rootCause === 'session_warmup' && (
              <>
                <span className="metric-tag">🌱 Session Warm-Up</span>
                <strong>{uncachedTokens != null ? number(uncachedTokens) : '—'} tokens uncached</strong>
                <span className="call-note">turn #{callSeq || 1} (pre-cache threshold)</span>
              </>
            )}
            {rootCause === 'transient_miss' && (
              <>
                <span className="metric-tag">💨 Cache Miss</span>
                <strong>{uncachedTokens != null ? number(uncachedTokens) : '—'} tokens uncached</strong>
                <span className="call-note">
                  {elapsedFormatted ? `${elapsedFormatted} after cached turn` : 'transient provider eviction'}
                  {prevCacheRead ? ` (${number(prevCacheRead)} cached earlier)` : ''}
                </span>
              </>
            )}
            {rootCause === 'unstable_prefix' && (
              <>
                <span className="metric-tag">⚡ Unstable Prefix</span>
                <strong>{uncachedTokens != null ? number(uncachedTokens) : '—'} tokens uncached</strong>
                <span className="call-note">turn #{callSeq || 1} without cache reuse</span>
              </>
            )}
            {!rootCause && (
              <>
                <span className="metric-tag">⚡ Uncached Input</span>
                <strong>{uncachedTokens != null ? number(uncachedTokens) : '—'} tokens</strong>
                <span className="call-note">(threshold: 16k)</span>
              </>
            )}
          </div>
        )}

        <div className="finding-explanation-box">
          <div className="explanation-section">
            <div className="explanation-header">
              <span className="explanation-icon">💡</span>
              <strong>Analysis & Recommended Action</strong>
            </div>
            <p className="recommendation-text">{finding.recommendation}</p>
          </div>

          {isContextGrowth && (
            <div className="explanation-tips">
              <strong>Why this happens & what you can do:</strong>
              <ul>
                <li>
                  <strong>Why it matters:</strong> As an agent reads long terminal outputs or entire files, each
                  subsequent message re-sends that entire accumulated history, increasing cost and response latency.
                </li>
                <li>
                  <strong>Action:</strong> If you are done with that task, start a fresh session (<code>/new</code>) to
                  reset context, or compact conversation history.
                </li>
              </ul>
            </div>
          )}
        </div>

        <details>
          <summary>Inspect rule evidence</summary>
          <pre>{JSON.stringify(ev, null, 2)}</pre>
        </details>
      </div>
      <button className="text-button finding-action-btn" onClick={() => void openSession(finding.session_id)}>
        View session ↗
      </button>
    </article>
  );
}

function App() {
  const [page, setPage] = useState<Page>('Overview');
  const [agent, setAgent] = useState('');
  const [query, setQuery] = useState('');
  const [overview, setOverview] = useState<Overview | null>(null);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [findings, setFindings] = useState<Finding[]>([]);
  const [adapters, setAdapters] = useState<Adapter[]>([]);
  const [reports, setReports] = useState<Report[]>([]);
  const [selected, setSelected] = useState<SessionDetail | null>(null);
  const [events, setEvents] = useState<CanonicalEvent[]>([]);
  const [detailTab, setDetailTab] = useState('Prompts');
  const [reportBody, setReportBody] = useState('');
  const [reportFormat, setReportFormat] = useState<'formatted' | 'raw'>('formatted');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [connected, setConnected] = useState(false);
  const [updated, setUpdated] = useState<Date | null>(null);
  const [offset, setOffset] = useState(0);

  const refresh = useCallback(
    async (signal?: AbortSignal) => {
      try {
        const params = new URLSearchParams({
          ...(agent ? { agent } : {}),
          q: query,
          offset: String(offset),
          limit: '25',
        });
        const [summary, rows, rules, capabilities, saved] = await Promise.all([
          api<Overview>(`/overview?${params}`, { signal }),
          api<Session[]>(`/sessions?${params}`, { signal }),
          api<Finding[]>(`/findings?${params}`, { signal }),
          api<Adapter[]>('/adapters', { signal }),
          api<Report[]>('/reports', { signal }),
        ]);
        setOverview(summary);
        setSessions(rows);
        setFindings(rules);
        setAdapters(capabilities);
        setReports(saved);
        setConnected(true);
        setUpdated(new Date());
        setError('');
      } catch (e) {
        if (e instanceof Error && e.name !== 'AbortError') {
          setError(e.message);
          setConnected(false);
        }
      }
    },
    [agent, query, offset]
  );

  useEffect(() => {
    const controller = new AbortController();
    const timer = window.setTimeout(() => void refresh(controller.signal), 180);
    const interval = window.setInterval(() => void refresh(controller.signal), 10000);
    return () => {
      controller.abort();
      clearTimeout(timer);
      clearInterval(interval);
    };
  }, [refresh]);

  useEffect(() => {
    setOffset(0);
  }, [agent, query]);

  useEffect(() => {
    const close = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setSelected(null);
        setReportBody('');
      }
    };
    window.addEventListener('keydown', close);
    return () => window.removeEventListener('keydown', close);
  }, []);

  useEffect(() => {
    if (!selected && !reportBody) return;
    const previous = document.activeElement as HTMLElement | null;
    const dialog = document.querySelector<HTMLElement>('[role="dialog"]');
    const controls = () =>
      Array.from(dialog?.querySelectorAll<HTMLElement>('button, a, input, summary, [tabindex="0"]') || []);
    controls()[0]?.focus();
    const trap = (event: KeyboardEvent) => {
      if (event.key !== 'Tab') return;
      const items = controls();
      const first = items[0];
      const last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last?.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first?.focus();
      }
    };
    document.addEventListener('keydown', trap);
    const overflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.removeEventListener('keydown', trap);
      document.body.style.overflow = overflow;
      previous?.focus();
    };
  }, [selected, reportBody]);

  async function openSession(id: string) {
    try {
      const [detail, timeline] = await Promise.all([
        api<SessionDetail>(`/sessions/${id}`),
        api<CanonicalEvent[]>(`/events?session_id=${id}`),
      ]);
      setSelected(detail);
      setEvents(timeline);
      setDetailTab('Prompts');
    } catch (e) {
      setError(String(e));
    }
  }

  async function makeReport() {
    setBusy(true);
    try {
      const end = new Date();
      const start = new Date(end.getTime() - 86400000);
      const result = await api<{ body: string }>('/reports', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ start: start.toISOString(), end: end.toISOString() }),
      });
      setReportBody(result.body);
      setReportFormat('formatted');
      await refresh();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function openReport(id: string) {
    try {
      const result = await api<Report>(`/reports/${id}`);
      setReportBody(result.body || '');
      setReportFormat('formatted');
    } catch (e) {
      setError(String(e));
    }
  }

  const tokenTotal =
    overview &&
    [
      overview.input_uncached,
      overview.input_cache_read,
      overview.input_cache_write,
      overview.output,
      overview.reasoning,
    ].some((v) => v != null)
      ? (overview.input_uncached || 0) +
        (overview.input_cache_read || 0) +
        (overview.input_cache_write || 0) +
        (overview.output || 0) +
        (overview.reasoning || 0)
      : null;

  const chartData = [...(overview?.daily || [])].reverse();

  const title = {
    Overview: 'A clear view of the work.',
    Sessions: 'Follow every thread.',
    Findings: 'Make the next run better.',
    Reports: 'The day, accounted for.',
    Coverage: 'Know what you can see.',
  }[page];

  const description = {
    Overview: 'Every agent. Every prompt. One local record.',
    Sessions: 'Explore prompts, tools, outcomes, and the evidence behind them.',
    Findings: 'Explainable signals from your agents’ activity. No model calls.',
    Reports: 'Consolidated reports, generated and stored on this machine.',
    Coverage: 'Capture capabilities vary by agent and version. Partial means partial.',
  }[page];

  return (
    <div className="app">
      <aside className="sidebar">
        <a
          className="brand"
          href="#"
          onClick={(e) => {
            e.preventDefault();
            setPage('Overview');
          }}
        >
          <span className="brand-mark">
            t<span>•</span>
          </span>
          tracebook
          <span className="version">LOCAL</span>
        </a>
        <div className="workspace">
          <span className="machine-icon">⌘</span>
          <div>
            This machine
            <small>Personal workspace</small>
          </div>
          <span className="workspace-dot" />
        </div>
        <div className="nav-label">OBSERVATORY</div>
        <nav aria-label="Main navigation">
          {(['Overview', 'Sessions', 'Findings', 'Reports', 'Coverage'] as Page[]).map((item, i) => (
            <button
              key={item}
              aria-label={item}
              className={page === item ? 'active' : ''}
              onClick={() => setPage(item)}
            >
              <span className="nav-icon">{['◫', '≡', '⌁', '▤', '◈'][i]}</span>
              {item}
              {item === 'Findings' && !!overview?.findings && (
                <span className="nav-count">{overview.findings}</span>
              )}
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="private-note">
            <span>◉</span>
            <strong>Local by design.</strong>
            <p>
              Your activity stays here.
              <br />
              Zero extra model tokens.
            </p>
          </div>
          <div className="connection">
            <i className={connected ? 'online' : ''} />
            {connected ? 'Daemon connected' : 'Connecting to daemon'}
            <span>v0.1</span>
          </div>
        </div>
      </aside>
      <main>
        <header className="topbar">
          <div>
            Workspace <span>/</span> <strong>{page}</strong>
          </div>
          <div className="top-meta">
            <span className="live-dot" />
            {updated
              ? `Updated ${updated.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })}`
              : 'Waiting for capture'}
            <button className="icon-button" aria-label="Refresh activity" onClick={() => void refresh()}>
              ↻
            </button>
          </div>
        </header>
        <div className="content">
          <div className="page-heading">
            <div>
              <div className="eyebrow">YOUR AGENTS, IN PERSPECTIVE</div>
              <h1>{title}</h1>
              <p>{description}</p>
            </div>
            <button className="primary" onClick={() => void makeReport()} disabled={busy}>
              <span>↗</span>
              {busy ? 'Generating…' : 'Generate report'}
            </button>
          </div>

          {error && (
            <div className="error" role="alert">
              {error}
              <button onClick={() => void refresh()}>Retry</button>
            </div>
          )}

          {['Overview', 'Sessions', 'Findings'].includes(page) && (
            <div className="filterbar">
              <div className="agent-filters" role="group" aria-label="Filter by agent">
                <button className={!agent ? 'selected' : ''} onClick={() => setAgent('')}>
                  All agents
                </button>
                {adapters
                  .filter((a) => a.id !== 'generic')
                  .map((a) => (
                    <button
                      key={a.id}
                      className={agent === a.id ? 'selected' : ''}
                      onClick={() => setAgent(a.id)}
                    >
                      <span className={`tiny-dot ${a.id}`} />
                      {a.name}
                    </button>
                  ))}
              </div>
              <span className="range">All captured activity</span>
            </div>
          )}

          {page === 'Overview' && (
            <>
              <div className="guide-card">
                <div className="guide-header">
                  <span className="guide-badge">💡 Quick Guide</span>
                  <h3>How to read your dashboard</h3>
                </div>
                <p className="guide-lead">
                  Tracebook records your coding assistants (such as <strong>Antigravity</strong> and{' '}
                  <strong>Claude Code</strong>) privately on your computer. Here is what the key metrics mean:
                </p>
                <div className="guide-grid">
                  <div className="guide-item">
                    <span className="guide-icon">💬</span>
                    <div>
                      <strong>Prompts & Turns</strong>
                      <p>Questions, instructions, or tasks you handed to an agent.</p>
                    </div>
                  </div>
                  <div className="guide-item">
                    <span className="guide-icon">⚡</span>
                    <div>
                      <strong>Tokens Processed</strong>
                      <p>Units of text & code read or generated (approx. 750 words per 1k tokens).</p>
                    </div>
                  </div>
                  <div className="guide-item">
                    <span className="guide-icon">🛠️</span>
                    <div>
                      <strong>Tools Executed</strong>
                      <p>Actions like reading files, writing code, terminal commands, or web lookups.</p>
                    </div>
                  </div>
                  <div className="guide-item">
                    <span className="guide-icon">💰</span>
                    <div>
                      <strong>Estimated Value</strong>
                      <p>Equivalent raw API price benchmark (does not replace subscription billing).</p>
                    </div>
                  </div>
                </div>
              </div>

              <div className="stats">
                <Stat
                  label="PROMPTS CAPTURED"
                  value={number(overview?.turns)}
                  note={`Questions & tasks across ${number(overview?.sessions)} sessions`}
                  spark="↗"
                />
                <Stat
                  label="TOKENS OBSERVED"
                  value={number(tokenTotal)}
                  note={`${number(overview?.input_cache_read)} read from fast prompt cache`}
                  spark="◈"
                />
                <Stat
                  label="REPORTED API VALUE"
                  value={money(overview?.cost_reported)}
                  note={`${money(overview?.cost_estimated)} estimated separately`}
                />
                <Stat
                  label="SIGNALS TO REVIEW"
                  value={number(overview?.findings)}
                  note={`${number(overview?.unpriced_calls || 0)} model calls without a price`}
                  accent
                />
              </div>

              <div className="overview-grid">
                <section className="panel activity-panel">
                  <div className="section-heading">
                    <div>
                      <span className="eyebrow">THE BIG PICTURE</span>
                      <h2>Activity over time</h2>
                    </div>
                    <span className="legend">
                      <i />
                      Tokens observed
                    </span>
                  </div>
                  {chartData.length ? (
                    <div className="chart">
                      <ResponsiveContainer width="100%" height="100%">
                        <AreaChart data={chartData} margin={{ top: 15, right: 16, left: 0, bottom: 0 }}>
                          <defs>
                            <linearGradient id="tokenFill" x1="0" y1="0" x2="0" y2="1">
                              <stop offset="0%" stopColor="#467d34" stopOpacity={0.25} />
                              <stop offset="100%" stopColor="#467d34" stopOpacity={0.02} />
                            </linearGradient>
                          </defs>
                          <CartesianGrid stroke="#e4edd9" strokeDasharray="3 5" vertical={false} />
                          <XAxis
                            dataKey="day"
                            tick={{ fontSize: 12, fill: '#556c4a' }}
                            tickFormatter={(v) =>
                              new Date(String(v) + 'T12:00:00').toLocaleDateString(undefined, {
                                month: 'short',
                                day: 'numeric',
                              })
                            }
                            axisLine={false}
                            tickLine={false}
                            minTickGap={40}
                          />
                          <YAxis
                            tickFormatter={(v) => number(Number(v))}
                            tick={{ fontSize: 12, fill: '#556c4a' }}
                            axisLine={false}
                            tickLine={false}
                            width={48}
                          />
                          <Tooltip
                            formatter={(v) => [number(Number(v)), 'Tokens']}
                            contentStyle={{
                              background: '#ffffff',
                              border: '1px solid #cbd8bf',
                              borderRadius: 8,
                              fontSize: 13,
                              color: '#1a2c14',
                              boxShadow: '0 4px 12px rgba(26, 44, 20, 0.1)',
                            }}
                          />
                          <Area
                            isAnimationActive={false}
                            type="monotone"
                            dataKey="tokens"
                            stroke="#3d6f2d"
                            fill="url(#tokenFill)"
                            strokeWidth={2.5}
                            dot={chartData.length < 3}
                          />
                        </AreaChart>
                      </ResponsiveContainer>
                    </div>
                  ) : (
                    <Empty title="Your timeline starts here">
                      <p>
                        Run <code>tracebook backfill</code> to discover existing sessions.
                        <br />
                        New activity appears as your agents work.
                      </p>
                    </Empty>
                  )}
                  <div className="chart-footer">
                    <span>● Recorded locally</span>
                    <span>Missing usage is shown as unknown</span>
                  </div>
                </section>

                <section className="panel agent-panel">
                  <div className="section-heading">
                    <div>
                      <span className="eyebrow">WORK DISTRIBUTION</span>
                      <h2>Across your agents</h2>
                    </div>
                    <span className="subtle">↗</span>
                  </div>
                  {overview?.agents.length ? (
                    overview.agents.map((a) => (
                      <div className="agent-row" key={a.agent}>
                        <Mark agent={a.agent} />
                        <div className="agent-row-main">
                          <strong>{names[a.agent] || a.agent}</strong>
                          <span>{number(a.calls)} model calls</span>
                          <div className="meter">
                            <i
                              style={{
                                width: `${Math.max(
                                  3,
                                  (a.tokens / Math.max(1, tokenTotal || 0)) * 100
                                )}%`,
                              }}
                            />
                          </div>
                        </div>
                        <div className="agent-row-value">
                          {number(a.tokens)}
                          <span>tokens</span>
                        </div>
                      </div>
                    ))
                  ) : (
                    <div className="agent-placeholder">
                      {adapters
                        .filter((a) => a.id !== 'generic')
                        .map((a) => (
                          <div key={a.id}>
                            <Mark agent={a.id} />
                            <span>
                              {a.name}
                              <small>No captured usage yet</small>
                            </span>
                          </div>
                        ))}
                    </div>
                  )}
                  <button className="text-button coverage-link" onClick={() => setPage('Coverage')}>
                    Explore capture coverage <span>→</span>
                  </button>
                </section>
              </div>

              <div className="section-title">
                <h2>
                  Recent sessions <span>{number(overview?.sessions)}</span>
                </h2>
                <button className="text-button" onClick={() => setPage('Sessions')}>
                  View all sessions →
                </button>
              </div>
              <SessionTable sessions={sessions.slice(0, 6)} open={openSession} />

              {!!findings.length && (
                <div className="signal-strip">
                  <span className="signal-star">✳</span>
                  <div>
                    <strong>{findings[0].title}</strong>
                    <p>{findings[0].recommendation}</p>
                  </div>
                  <button className="text-button" onClick={() => setPage('Findings')}>
                    Review signals →
                  </button>
                </div>
              )}

              <p className="billing-note">
                Values are API-equivalent estimates or agent-reported figures. For subscription plans, they do not
                represent billed spend.
              </p>
            </>
          )}

          {page === 'Sessions' && (
            <>
              <div className="search-row">
                <label>
                  <span>⌕</span>
                  <input
                    type="search"
                    placeholder="Search prompts…"
                    aria-label="Search prompts"
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                  />
                </label>
                <span className="subtle">Select a session to inspect its timeline</span>
              </div>
              <SessionTable sessions={sessions} open={openSession} />
              <div className="pagination">
                <button disabled={!offset} onClick={() => setOffset(Math.max(0, offset - 25))}>
                  ← Previous
                </button>
                <span>Page {offset / 25 + 1}</span>
                <button disabled={sessions.length < 25} onClick={() => setOffset(offset + 25)}>
                  Next →
                </button>
              </div>
            </>
          )}

          {page === 'Findings' && (
            <div className="findings-list">
              {findings.length ? (
                findings.map((f) => <FindingCard key={f.id} finding={f} openSession={openSession} />)
              ) : (
                <Empty title="No signals to review">
                  <p>No rules have triggered for this selection. Coverage determines what can be detected.</p>
                </Empty>
              )}
            </div>
          )}

          {page === 'Coverage' && (
            <>
              <div className="coverage-intro">
                <span>◈</span>
                <p>
                  Transcripts, hooks, and telemetry complement one another.{' '}
                  <strong>Partial</strong> flags incomplete data or an adapter not yet verified on this machine.
                </p>
              </div>
              <div className="panel coverage-table">
                <table>
                  <thead>
                    <tr>
                      <th>Capability</th>
                      {adapters.map((a) => (
                        <th key={a.id}>
                          <Mark agent={a.id} />
                          {a.name}
                          <small>{a.verification}</small>
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {Object.keys(adapters[0]?.capabilities || {}).map((key) => (
                      <tr key={key}>
                        <td>{key.replaceAll('_', ' ')}</td>
                        {adapters.map((a) => (
                          <td key={a.id}>
                            <span className={`capability ${a.capabilities[key]}`}>
                              {a.capabilities[key] === 'yes'
                                ? '✓ Supported'
                                : a.capabilities[key] === 'partial'
                                  ? '◐ Partial'
                                  : '— Unavailable'}
                            </span>
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}

          {page === 'Reports' && (
            <>
              <div className="report-note panel">
                <span>▤</span>
                <div>
                  <h2>A record you can return to.</h2>
                  <p>
                    Nightly reports are stored locally. Delivery runs only through channels you configure.
                    Generate a report above for the last 24 hours.
                  </p>
                </div>
              </div>

              <div className="token-guide-card panel">
                <div className="token-guide-header">
                  <span className="token-guide-tag">📖 TOKEN METRICS GUIDE</span>
                  <h3>Understanding token categories in reports</h3>
                  <p>
                    AI providers categorize tokens into different pools with distinct cost rates and performance profiles:
                  </p>
                </div>
                <div className="token-guide-grid">
                  <div className="token-guide-item">
                    <div className="token-guide-title">
                      <span className="token-indicator uncached" />
                      <strong>Input uncached</strong>
                    </div>
                    <p>
                      Brand new text, project files, and conversation history that the AI processes from scratch.
                      Billed at standard input rates.
                    </p>
                  </div>
                  <div className="token-guide-item">
                    <div className="token-guide-title">
                      <span className="token-indicator cache-read" />
                      <strong>Cache read</strong>
                    </div>
                    <p>
                      Reused instructions and earlier turns loaded directly from prompt cache. Processes faster and is
                      typically <strong>75%–90% cheaper</strong>.
                    </p>
                  </div>
                  <div className="token-guide-item">
                    <div className="token-guide-title">
                      <span className="token-indicator cache-write" />
                      <strong>Cache write</strong>
                    </div>
                    <p>
                      Context stored into the provider's prompt cache for the first time during a turn (supported by
                      Anthropic Claude).
                    </p>
                    <small className="token-guide-subnote">
                      <em>Note for Antigravity:</em> Google Gemini uses automatic prefix caching without a separate
                      cache-write fee or counter (shown as unavailable).
                    </small>
                  </div>
                  <div className="token-guide-item">
                    <div className="token-guide-title">
                      <span className="token-indicator output" />
                      <strong>Output</strong>
                    </div>
                    <p>
                      Words, code completions, and tool call instructions generated by the AI model in response to your
                      prompts. Usually billed at higher rates.
                    </p>
                  </div>
                  <div className="token-guide-item">
                    <div className="token-guide-title">
                      <span className="token-indicator reasoning" />
                      <strong>Reasoning</strong>
                    </div>
                    <p>
                      Internal &ldquo;thinking&rdquo; tokens used by reasoning models (e.g. Gemini Thinking, Claude
                      Extended Thinking) to plan before answering.
                    </p>
                  </div>
                </div>
              </div>

              {reports.length ? (
                <div className="reports-list">
                  {reports.map((r) => (
                    <button className="panel report-card" key={r.id} onClick={() => void openReport(r.id)}>
                      <span className="report-icon">▤</span>
                      <div>
                        <strong>
                          {date(r.window_start)} → {date(r.window_end)}
                        </strong>
                        <small>Consolidated activity · coverage and evidence included</small>
                      </div>
                      <span>↗</span>
                    </button>
                  ))}
                </div>
              ) : (
                <Empty title="No reports yet">
                  <p>Your first report will appear here after generation.</p>
                </Empty>
              )}
            </>
          )}

          <footer>
            <span>
              TRACEBOOK <span className="footer-dot">/</span> A little clarity for autonomous work.
            </span>
            <span>LOCAL · PRIVATE · AGENT-AGNOSTIC</span>
          </footer>
        </div>
      </main>

      {selected && (
        <div className="overlay" onClick={() => setSelected(null)}>
          <section
            className="drawer"
            role="dialog"
            aria-modal="true"
            aria-label="Session details"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="drawer-header">
              <Mark agent={selected.agent} />
              <div>
                <span className="eyebrow">SESSION DETAILS</span>
                <h2>{names[selected.agent] || selected.agent}</h2>
              </div>
              <button className="icon-button" aria-label="Close session" onClick={() => setSelected(null)}>
                ×
              </button>
            </div>
            <div className="session-id-box">
              <span className="session-id-label">Session ID:</span>
              <code className="session-id">{selected.native_id}</code>
            </div>
            <div className="detail-tabs">
              {['Prompts', 'Tools', 'Evidence'].map((tab) => (
                <button
                  className={detailTab === tab ? 'active' : ''}
                  key={tab}
                  onClick={() => setDetailTab(tab)}
                >
                  {tab}
                </button>
              ))}
            </div>
            <div className="drawer-body">
              {detailTab === 'Prompts' &&
                selected.turns.map((turn, i) => <PromptCard key={turn.id} turn={turn} index={i} />)}
              {detailTab === 'Tools' && selected.tools.map((tool) => <ToolCard key={tool.id} tool={tool} />)}
              {detailTab === 'Evidence' && (
                <>
                  <p className="subtle">
                    First {events.length} canonical events. Fields show where their values came from.
                  </p>
                  {events.map((event) => (
                    <details className="evidence" key={event.id}>
                      <summary>
                        <strong>{event.kind.replaceAll('_', ' ')}</strong>
                        <time>{date(event.ts)}</time>
                      </summary>
                      <pre>
                        {JSON.stringify(
                          {
                            data: event.data,
                            provenance: event.provenance,
                          },
                          null,
                          2
                        )}
                      </pre>
                    </details>
                  ))}
                </>
              )}
            </div>
          </section>
        </div>
      )}

      {reportBody && (
        <div className="overlay" onClick={() => setReportBody('')}>
          <section
            className="report-modal"
            role="dialog"
            aria-modal="true"
            aria-label="Generated report"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="drawer-header report-modal-header">
              <div>
                <span className="eyebrow">CONSOLIDATED REPORT</span>
                <h2>Your activity report</h2>
              </div>
              <div className="report-toggle-bar" role="group" aria-label="Report format">
                <button
                  type="button"
                  className={reportFormat === 'formatted' ? 'active' : ''}
                  onClick={() => setReportFormat('formatted')}
                >
                  📄 Formatted
                </button>
                <button
                  type="button"
                  className={reportFormat === 'raw' ? 'active' : ''}
                  onClick={() => setReportFormat('raw')}
                >
                  📝 Raw Markdown
                </button>
              </div>
              <button className="icon-button" aria-label="Close report" onClick={() => setReportBody('')}>
                ×
              </button>
            </div>

            <details className="report-token-quickhelp">
              <summary>💡 What do Input uncached, Cache read, Output, and Reasoning mean?</summary>
              <div className="report-token-quickhelp-grid">
                <div>
                  <strong>Input uncached:</strong> Fresh text and files processed from scratch.
                </div>
                <div>
                  <strong>Cache read:</strong> Cached context (faster &amp; 75–90% cheaper).
                </div>
                <div>
                  <strong>Cache write:</strong> New context saved to cache (used by Anthropic; Gemini uses automatic
                  caching).
                </div>
                <div>
                  <strong>Output:</strong> Content and code generated by the model.
                </div>
                <div>
                  <strong>Reasoning:</strong> Internal thinking tokens used by reasoning models.
                </div>
              </div>
            </details>

            <div className="report-modal-content">
              {reportFormat === 'formatted' ? (
                <div
                  className="report-rendered"
                  dangerouslySetInnerHTML={{
                    __html: marked.parse(reportBody, { async: false }) as string,
                  }}
                />
              ) : (
                <pre className="report-raw">{reportBody}</pre>
              )}
            </div>

            <div className="report-modal-footer">
              <button
                className="primary"
                onClick={() => {
                  const url = URL.createObjectURL(new Blob([reportBody], { type: 'text/markdown' }));
                  const a = document.createElement('a');
                  a.href = url;
                  a.download = 'tracebook-report.md';
                  a.click();
                  URL.revokeObjectURL(url);
                }}
              >
                Download Markdown ↓
              </button>
              <button
                className="text-button copy-report-btn"
                onClick={() => {
                  void navigator.clipboard.writeText(reportBody);
                }}
              >
                Copy Markdown
              </button>
            </div>
          </section>
        </div>
      )}
    </div>
  );
}

function Stat({
  label,
  value,
  note,
  spark,
  accent,
}: {
  label: string;
  value: string;
  note: string;
  spark?: string;
  accent?: boolean;
}) {
  return (
    <div className={`stat ${accent ? 'accent' : ''}`}>
      <span className="stat-label">{label}</span>
      <div className="stat-value">
        {value}
        {spark && (
          <span className="spark" aria-hidden="true">
            {spark}
          </span>
        )}
        {accent && <span className="stat-star">✳</span>}
      </div>
      <p>{note}</p>
    </div>
  );
}

function SessionTable({ sessions, open }: { sessions: Session[]; open: (id: string) => Promise<void> }) {
  return (
    <div className="panel session-table">
      {sessions.length ? (
        <table>
          <thead>
            <tr>
              <th>Agent / prompt</th>
              <th>Project</th>
              <th>Activity</th>
              <th>Tokens</th>
              <th>Started</th>
              <th>
                <span className="sr-only">Open</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {sessions.map((s) => (
              <tr key={s.id}>
                <td>
                  <button className="session-button" onClick={() => void open(s.id)}>
                    <Mark agent={s.agent} />
                    <span>
                      <strong>{s.first_prompt?.slice(0, 95) || names[s.agent] || s.agent}</strong>
                      <small>
                        {names[s.agent] || s.agent}
                        {s.resumed ? ' · resumed' : ''}
                      </small>
                    </span>
                  </button>
                </td>
                <td>
                  <span className="project-label">
                    {s.project?.split('/').filter(Boolean).pop() || 'Unattributed'}
                  </span>
                </td>
                <td className="activity-count">
                  {number(s.turns)} prompts <span>·</span> {number(s.tools)} tools
                </td>
                <td className="numeric">{number(s.tokens)}</td>
                <td className="timestamp">{date(s.started_at)}</td>
                <td>
                  <button
                    className="icon-button"
                    aria-label={`Open ${names[s.agent] || s.agent} session`}
                    onClick={() => void open(s.id)}
                  >
                    ↗
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <Empty title="Ready when you are.">
          <p>
            Captured sessions appear here automatically.
            <br />
            Use <code>tracebook doctor</code> to check your agent connections.
          </p>
        </Empty>
      )}
    </div>
  );
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>
);
