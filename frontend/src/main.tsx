import { StrictMode, useCallback, useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import type { Adapter, CanonicalEvent, Finding, Overview, Report, Session, SessionDetail } from './types';
import { PromptCard, ToolCard } from './PromptCard';
import './style.css';

type Page = 'Overview' | 'Sessions' | 'Findings' | 'Reports' | 'Coverage';
const names: Record<string, string> = { claude_code: 'Claude Code', codex: 'OpenAI Codex', gemini_cli: 'Gemini CLI', generic: 'Generic' };
const short: Record<string, string> = { claude_code: 'Cl', codex: 'Cx', gemini_cli: 'Ge', generic: '·' };
const number = (v: number | null | undefined) => v == null ? '—' : new Intl.NumberFormat('en', { notation: v > 9999 ? 'compact' : 'standard', maximumFractionDigits: 1 }).format(v);
const money = (v: number | null | undefined) => v == null ? '—' : new Intl.NumberFormat('en', { style: 'currency', currency: 'USD', maximumFractionDigits: 3 }).format(v);
const date = (v: string | null) => !v || v.startsWith('1970') ? 'Time unavailable' : new Date(v).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, options);
  if (!response.ok) throw new Error(`Request failed (${response.status}). Check that the local daemon is running.`);
  return response.json() as Promise<T>;
}
function Mark({ agent }: { agent: string }) { return <span className={`agent-mark ${agent}`}>{short[agent] || agent.slice(0, 2)}</span>; }
function Status({ value }: { value: string }) { return <span className={`status ${value}`}><i />{value}</span>; }
function Empty({ title, children }: { title: string; children: React.ReactNode }) { return <div className="empty"><span className="empty-icon">⌁</span><h3>{title}</h3><div>{children}</div></div>; }

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
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [connected, setConnected] = useState(false);
  const [updated, setUpdated] = useState<Date | null>(null);
  const [offset, setOffset] = useState(0);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    try {
      const params = new URLSearchParams({ ...(agent ? { agent } : {}), q: query, offset: String(offset), limit: '25' });
      const [summary, rows, rules, capabilities, saved] = await Promise.all([
        api<Overview>(`/overview?${params}`, { signal }), api<Session[]>(`/sessions?${params}`, { signal }),
        api<Finding[]>(`/findings?${params}`, { signal }), api<Adapter[]>('/adapters', { signal }), api<Report[]>('/reports', { signal }),
      ]);
      setOverview(summary); setSessions(rows); setFindings(rules); setAdapters(capabilities); setReports(saved);
      setConnected(true); setUpdated(new Date()); setError('');
    } catch (e) {
      if (e instanceof Error && e.name !== 'AbortError') { setError(e.message); setConnected(false); }
    }
  }, [agent, query, offset]);
  useEffect(() => {
    const controller = new AbortController();
    const timer = window.setTimeout(() => void refresh(controller.signal), 180);
    const interval = window.setInterval(() => void refresh(controller.signal), 10000);
    return () => { controller.abort(); clearTimeout(timer); clearInterval(interval); };
  }, [refresh]);
  useEffect(() => { setOffset(0); }, [agent, query]);
  useEffect(() => {
    const close = (event: KeyboardEvent) => { if (event.key === 'Escape') { setSelected(null); setReportBody(''); } };
    window.addEventListener('keydown', close); return () => window.removeEventListener('keydown', close);
  }, []);

  useEffect(() => {
    if (!selected && !reportBody) return;
    const previous = document.activeElement as HTMLElement | null;
    const dialog = document.querySelector<HTMLElement>('[role="dialog"]');
    const controls = () => Array.from(dialog?.querySelectorAll<HTMLElement>('button, a, input, summary, [tabindex="0"]') || []);
    controls()[0]?.focus();
    const trap = (event: KeyboardEvent) => {
      if (event.key !== 'Tab') return;
      const items = controls(); const first = items[0]; const last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    };
    document.addEventListener('keydown', trap);
    const overflow = document.body.style.overflow; document.body.style.overflow = 'hidden';
    return () => { document.removeEventListener('keydown', trap); document.body.style.overflow = overflow; previous?.focus(); };
  }, [selected, reportBody]);

  async function openSession(id: string) {
    try {
      const [detail, timeline] = await Promise.all([api<SessionDetail>(`/sessions/${id}`), api<CanonicalEvent[]>(`/events?session_id=${id}`)]);
      setSelected(detail); setEvents(timeline); setDetailTab('Prompts');
    } catch (e) { setError(String(e)); }
  }
  async function makeReport() {
    setBusy(true);
    try {
      const end = new Date(); const start = new Date(end.getTime() - 86400000);
      const result = await api<{ body: string }>('/reports', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ start: start.toISOString(), end: end.toISOString() }) });
      setReportBody(result.body); await refresh();
    } catch (e) { setError(String(e)); } finally { setBusy(false); }
  }
  async function openReport(id: string) {
    try { const result = await api<Report>(`/reports/${id}`); setReportBody(result.body || ''); } catch (e) { setError(String(e)); }
  }
  const tokenTotal = overview && [overview.input_uncached, overview.input_cache_read, overview.input_cache_write, overview.output, overview.reasoning].some(v => v != null)
    ? (overview.input_uncached || 0) + (overview.input_cache_read || 0) + (overview.input_cache_write || 0) + (overview.output || 0) + (overview.reasoning || 0) : null;
  const chartData = [...(overview?.daily || [])].reverse();
  const title = { Overview: 'A clear view of the work.', Sessions: 'Follow every thread.', Findings: 'Make the next run better.', Reports: 'The day, accounted for.', Coverage: 'Know what you can see.' }[page];
  const description = { Overview: 'Every agent. Every prompt. One local record.', Sessions: 'Explore prompts, tools, outcomes, and the evidence behind them.', Findings: 'Explainable signals from your agents’ activity. No model calls.', Reports: 'Consolidated reports, generated and stored on this machine.', Coverage: 'Capture capabilities vary by agent and version. Partial means partial.' }[page];

  return <div className="app">
    <aside className="sidebar">
      <a className="brand" href="#" onClick={e => { e.preventDefault(); setPage('Overview'); }}><span className="brand-mark">t<span>•</span></span>tracebook<span className="version">LOCAL</span></a>
      <div className="workspace"><span className="machine-icon">⌘</span><div>This machine<small>Personal workspace</small></div><span className="workspace-dot" /></div>
      <div className="nav-label">OBSERVATORY</div>
      <nav aria-label="Main navigation">{(['Overview', 'Sessions', 'Findings', 'Reports', 'Coverage'] as Page[]).map((item, i) => <button key={item} aria-label={item} className={page === item ? 'active' : ''} onClick={() => setPage(item)}><span className="nav-icon">{['◫', '≡', '⌁', '▤', '◈'][i]}</span>{item}{item === 'Findings' && !!overview?.findings && <span className="nav-count">{overview.findings}</span>}</button>)}</nav>
      <div className="sidebar-bottom"><div className="private-note"><span>◉</span><strong>Local by design.</strong><p>Your activity stays here.<br />Zero extra model tokens.</p></div><div className="connection"><i className={connected ? 'online' : ''} />{connected ? 'Daemon connected' : 'Connecting to daemon'}<span>v0.1</span></div></div>
    </aside>
    <main>
      <header className="topbar"><div>Workspace <span>/</span> <strong>{page}</strong></div><div className="top-meta"><span className="live-dot" />{updated ? `Updated ${updated.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })}` : 'Waiting for capture'}<button className="icon-button" aria-label="Refresh activity" onClick={() => void refresh()}>↻</button></div></header>
      <div className="content"><div className="page-heading"><div><div className="eyebrow">YOUR AGENTS, IN PERSPECTIVE</div><h1>{title}</h1><p>{description}</p></div><button className="primary" onClick={() => void makeReport()} disabled={busy}><span>↗</span>{busy ? 'Generating…' : 'Generate report'}</button></div>
        {error && <div className="error" role="alert">{error}<button onClick={() => void refresh()}>Retry</button></div>}
        {['Overview', 'Sessions', 'Findings'].includes(page) && <div className="filterbar"><div className="agent-filters" role="group" aria-label="Filter by agent"><button className={!agent ? 'selected' : ''} onClick={() => setAgent('')}>All agents</button>{adapters.filter(a => a.id !== 'generic').map(a => <button key={a.id} className={agent === a.id ? 'selected' : ''} onClick={() => setAgent(a.id)}><span className={`tiny-dot ${a.id}`} />{a.name}</button>)}</div><span className="range">All captured activity</span></div>}
        {page === 'Overview' && <>
          <div className="stats"><Stat label="PROMPTS CAPTURED" value={number(overview?.turns)} note={`${number(overview?.sessions)} sessions across your agents`} spark="↗" /><Stat label="TOKENS OBSERVED" value={number(tokenTotal)} note={`${number(overview?.input_cache_read)} read from cache`} spark="◈" /><Stat label="REPORTED API VALUE" value={money(overview?.cost_reported)} note={`${money(overview?.cost_estimated)} estimated separately`} /><Stat label="SIGNALS TO REVIEW" value={number(overview?.findings)} note={`${number(overview?.unpriced_calls || 0)} model calls without a price`} accent /></div>
          <div className="overview-grid"><section className="panel activity-panel"><div className="section-heading"><div><span className="eyebrow">THE BIG PICTURE</span><h2>Activity over time</h2></div><span className="legend"><i />Tokens observed</span></div>{chartData.length ? <div className="chart"><ResponsiveContainer width="100%" height="100%"><AreaChart data={chartData} margin={{ top: 15, right: 16, left: 0, bottom: 0 }}><defs><linearGradient id="tokenFill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#637b52" stopOpacity={.22} /><stop offset="100%" stopColor="#637b52" stopOpacity={.01} /></linearGradient></defs><CartesianGrid stroke="#e8e8df" strokeDasharray="3 5" vertical={false} /><XAxis dataKey="day" tick={{ fontSize: 11, fill: '#7b7e74' }} tickFormatter={v => new Date(String(v) + 'T12:00:00').toLocaleDateString(undefined, { month: 'short', day: 'numeric' })} axisLine={false} tickLine={false} minTickGap={40} /><YAxis tickFormatter={v => number(Number(v))} tick={{ fontSize: 11, fill: '#7b7e74' }} axisLine={false} tickLine={false} width={45} /><Tooltip formatter={v => [number(Number(v)), 'Tokens']} contentStyle={{ background: '#fffef9', border: '1px solid #d9dccf', borderRadius: 8, fontSize: 12 }} /><Area isAnimationActive={false} type="monotone" dataKey="tokens" stroke="#637b52" fill="url(#tokenFill)" strokeWidth={2} dot={chartData.length < 3} /></AreaChart></ResponsiveContainer></div> : <Empty title="Your timeline starts here"><p>Run <code>tracebook backfill</code> to discover existing sessions.<br />New activity appears as your agents work.</p></Empty>}<div className="chart-footer"><span>● Recorded locally</span><span>Missing usage is shown as unknown</span></div></section>
          <section className="panel agent-panel"><div className="section-heading"><div><span className="eyebrow">WORK DISTRIBUTION</span><h2>Across your agents</h2></div><span className="subtle">↗</span></div>{overview?.agents.length ? overview.agents.map(a => <div className="agent-row" key={a.agent}><Mark agent={a.agent} /><div className="agent-row-main"><strong>{names[a.agent] || a.agent}</strong><span>{number(a.calls)} model calls</span><div className="meter"><i style={{ width: `${Math.max(2, a.tokens / Math.max(1, tokenTotal || 0) * 100)}%` }} /></div></div><div className="agent-row-value">{number(a.tokens)}<span>tokens</span></div></div>) : <div className="agent-placeholder">{adapters.filter(a => a.id !== 'generic').map(a => <div key={a.id}><Mark agent={a.id} /><span>{a.name}<small>No captured usage yet</small></span></div>)}</div>}<button className="text-button coverage-link" onClick={() => setPage('Coverage')}>Explore capture coverage <span>→</span></button></section></div>
          <div className="section-title"><h2>Recent sessions <span>{number(overview?.sessions)}</span></h2><button className="text-button" onClick={() => setPage('Sessions')}>View all sessions →</button></div><SessionTable sessions={sessions.slice(0, 6)} open={openSession} />
          {!!findings.length && <div className="signal-strip"><span className="signal-star">✳</span><div><strong>{findings[0].title}</strong><p>{findings[0].recommendation}</p></div><button className="text-button" onClick={() => setPage('Findings')}>Review signals →</button></div>}
          <p className="billing-note">Values are API-equivalent estimates or agent-reported figures. For subscription plans, they do not represent billed spend.</p>
        </>}
        {page === 'Sessions' && <><div className="search-row"><label><span>⌕</span><input type="search" placeholder="Search prompts…" aria-label="Search prompts" value={query} onChange={e => setQuery(e.target.value)} /></label><span className="subtle">Select a session to inspect its timeline</span></div><SessionTable sessions={sessions} open={openSession} /><div className="pagination"><button disabled={!offset} onClick={() => setOffset(Math.max(0, offset - 25))}>← Previous</button><span>Page {offset / 25 + 1}</span><button disabled={sessions.length < 25} onClick={() => setOffset(offset + 25)}>Next →</button></div></>}
        {page === 'Findings' && <div className="findings-list">{findings.length ? findings.map(f => <article className="panel finding" key={f.id}><div className="finding-icon">✳</div><div className="finding-main"><div className="finding-meta"><Status value={f.severity} /><span>{names[f.agent] || f.agent} · {date(f.ts)}</span></div><h2>{f.title}</h2><p>{f.recommendation}</p><details><summary>Inspect rule evidence</summary><pre>{JSON.stringify(JSON.parse(f.evidence), null, 2)}</pre></details></div><button className="text-button" onClick={() => void openSession(f.session_id)}>View session ↗</button></article>) : <Empty title="No signals to review"><p>No rules have triggered for this selection. Coverage determines what can be detected.</p></Empty>}</div>}
        {page === 'Coverage' && <><div className="coverage-intro"><span>◈</span><p>Transcripts, hooks, and telemetry complement one another. <strong>Partial</strong> flags incomplete data or an adapter not yet verified on this machine.</p></div><div className="panel coverage-table"><table><thead><tr><th>Capability</th>{adapters.map(a => <th key={a.id}><Mark agent={a.id} />{a.name}<small>{a.verification}</small></th>)}</tr></thead><tbody>{Object.keys(adapters[0]?.capabilities || {}).map(key => <tr key={key}><td>{key.replaceAll('_', ' ')}</td>{adapters.map(a => <td key={a.id}><span className={`capability ${a.capabilities[key]}`}>{a.capabilities[key] === 'yes' ? '✓ Supported' : a.capabilities[key] === 'partial' ? '◐ Partial' : '— Unavailable'}</span></td>)}</tr>)}</tbody></table></div></>}
        {page === 'Reports' && <><div className="report-note panel"><span>▤</span><div><h2>A record you can return to.</h2><p>Nightly reports are stored locally. Delivery runs only through channels you configure. Generate a report above for the last 24 hours.</p></div></div>{reports.length ? <div className="reports-list">{reports.map(r => <button className="panel report-card" key={r.id} onClick={() => void openReport(r.id)}><span className="report-icon">▤</span><div><strong>{date(r.window_start)} → {date(r.window_end)}</strong><small>Consolidated activity · coverage and evidence included</small></div><span>↗</span></button>)}</div> : <Empty title="No reports yet"><p>Your first report will appear here after generation.</p></Empty>}</>}
        <footer><span>TRACEBOOK <span className="footer-dot">/</span> A little clarity for autonomous work.</span><span>LOCAL · PRIVATE · AGENT-AGNOSTIC</span></footer>
      </div>
    </main>
    {selected && <div className="overlay" onClick={() => setSelected(null)}><section className="drawer" role="dialog" aria-modal="true" aria-label="Session details" onClick={e => e.stopPropagation()}><div className="drawer-header"><Mark agent={selected.agent} /><div><span className="eyebrow">SESSION TRACE</span><h2>{names[selected.agent] || selected.agent}</h2></div><button className="icon-button" aria-label="Close session" onClick={() => setSelected(null)}>×</button></div><code className="session-id">{selected.native_id}</code><div className="detail-tabs">{['Prompts', 'Tools', 'Evidence'].map(tab => <button className={detailTab === tab ? 'active' : ''} key={tab} onClick={() => setDetailTab(tab)}>{tab}</button>)}</div><div className="drawer-body">{detailTab === 'Prompts' && selected.turns.map((turn, i) => <PromptCard key={turn.id} turn={turn} index={i} />)}{detailTab === 'Tools' && selected.tools.map(tool => <ToolCard key={tool.id} tool={tool} />)}{detailTab === 'Evidence' && <><p className="subtle">First {events.length} canonical events. Fields show where their values came from.</p>{events.map(event => <details className="evidence" key={event.id}><summary><strong>{event.kind.replaceAll('_', ' ')}</strong><time>{date(event.ts)}</time></summary><pre>{JSON.stringify({ data: event.data, provenance: event.provenance }, null, 2)}</pre></details>)}</>}</div></section></div>}
    {reportBody && <div className="overlay" onClick={() => setReportBody('')}><section className="report-modal" role="dialog" aria-modal="true" aria-label="Generated report" onClick={e => e.stopPropagation()}><div className="drawer-header"><h2>Your activity report</h2><button className="icon-button" aria-label="Close report" onClick={() => setReportBody('')}>×</button></div><pre>{reportBody}</pre><button className="primary" onClick={() => { const url = URL.createObjectURL(new Blob([reportBody], { type: 'text/markdown' })); const a = document.createElement('a'); a.href = url; a.download = 'tracebook-report.md'; a.click(); URL.revokeObjectURL(url); }}>Download Markdown ↓</button></section></div>}
  </div>;
}

function Stat({ label, value, note, spark, accent }: { label: string; value: string; note: string; spark?: string; accent?: boolean }) { return <div className={`stat ${accent ? 'accent' : ''}`}><span className="stat-label">{label}</span><div className="stat-value">{value}{spark && <span className="spark" aria-hidden="true">{spark}</span>}{accent && <span className="stat-star">✳</span>}</div><p>{note}</p></div>; }
function SessionTable({ sessions, open }: { sessions: Session[]; open: (id: string) => Promise<void> }) { return <div className="panel session-table">{sessions.length ? <table><thead><tr><th>Agent / prompt</th><th>Project</th><th>Activity</th><th>Tokens</th><th>Started</th><th><span className="sr-only">Open</span></th></tr></thead><tbody>{sessions.map(s => <tr key={s.id}><td><button className="session-button" onClick={() => void open(s.id)}><Mark agent={s.agent} /><span><strong>{s.first_prompt?.slice(0, 95) || names[s.agent] || s.agent}</strong><small>{names[s.agent] || s.agent}{s.resumed ? ' · resumed' : ''}</small></span></button></td><td><span className="project-label">{s.project?.split('/').filter(Boolean).pop() || 'Unattributed'}</span></td><td className="activity-count">{number(s.turns)} prompts <span>·</span> {number(s.tools)} tools</td><td className="numeric">{number(s.tokens)}</td><td className="timestamp">{date(s.started_at)}</td><td><button className="icon-button" aria-label={`Open ${names[s.agent] || s.agent} session`} onClick={() => void open(s.id)}>↗</button></td></tr>)}</tbody></table> : <Empty title="Ready when you are."><p>Captured sessions appear here automatically.<br />Use <code>tracebook doctor</code> to check your agent connections.</p></Empty>}</div>; }

createRoot(document.getElementById('root')!).render(<StrictMode><App /></StrictMode>);
