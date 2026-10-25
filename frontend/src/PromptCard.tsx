import { useEffect, useState } from 'react';
import type { ModelCall, Tool, Turn, TurnDetail } from './types';

const count = (value: number | null | undefined) => value == null ? '—' : value.toLocaleString();
const cost = (value: number | null) => value == null ? '—' : `$${value.toFixed(4)}`;
const elapsed = (value: unknown) => typeof value === 'number' && value >= 0 ? `${(value / 1000).toFixed(2)}s` : '—';
const tokenFields = [
  ['input_uncached', 'Input uncached'], ['input_cache_read', 'Cache read'],
  ['input_cache_write', 'Cache write'], ['output', 'Output'], ['reasoning', 'Reasoning'],
] as const;
function sum(values: (number | null)[]) {
  const known = values.filter((value): value is number => value != null);
  return known.length ? known.reduce((total, value) => total + value, 0) : null;
}

export function ToolCard({ tool }: { tool: Tool }) {
  return <details className="tool-card">
    <summary><span className="tool-kind">{tool.tool}</span><strong>{tool.native_name || 'Unknown tool'}</strong><span className={`status ${tool.status}`}>{tool.status}</span><span>{elapsed(tool.duration_ms)}</span></summary>
    <h4>Input</h4><pre>{tool.input || 'Not captured'}</pre>
    <h4>Output</h4><pre>{tool.output || 'Not captured'}</pre>
  </details>;
}

function CallCard({ call }: { call: ModelCall }) {
  const data = JSON.parse(call.data) as Record<string, unknown>;
  return <details className="call-card">
    <summary><strong>{call.model || 'Model unavailable'}</strong><span>{elapsed(data.duration_ms)}</span></summary>
    <dl className="prompt-metrics">
      {tokenFields.map(([field, label]) => <div key={field}><dt>{label}</dt><dd>{count(call[field])}</dd></div>)}
      <div><dt>Total reported</dt><dd>{count(call.total_reported)}</dd></div>
      <div><dt>Cost reported</dt><dd>{cost(call.cost_reported)}</dd></div>
      <div><dt>Cost estimated</dt><dd>{cost(call.cost_estimated)}</dd></div>
    </dl>
    <small>Token semantics: {String(data.token_semantics_confidence || 'not supplied')}</small>
    <details className="call-evidence"><summary>Captured call fields</summary><pre>{JSON.stringify(data, null, 2)}</pre></details>
  </details>;
}

export function PromptCard({ turn, index }: { turn: Turn; index: number }) {
  const [open, setOpen] = useState(false);
  const [detail, setDetail] = useState<TurnDetail | null>(null);
  const [error, setError] = useState('');
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    setError(''); setDetail(null);
    void fetch(`/api/turns/${encodeURIComponent(turn.id)}`, { signal: controller.signal })
      .then(response => { if (!response.ok) throw new Error('Prompt details could not be loaded.'); return response.json() as Promise<TurnDetail>; })
      .then(setDetail)
      .catch((reason: unknown) => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : 'Prompt details could not be loaded.'); });
    return () => controller.abort();
  }, [open, turn.id, retry]);
  const stamp = (value: string | null) => !value || value.startsWith('1970') ? 'Time unavailable' : new Date(value).toLocaleString();
  const duration = turn.started_at && turn.ended_at && !turn.started_at.startsWith('1970') && !turn.ended_at.startsWith('1970')
    ? Date.parse(turn.ended_at) - Date.parse(turn.started_at) : null;
  const unpriced = detail?.calls.filter(call => call.cost_reported == null && call.cost_estimated == null).length || 0;
  return <article className="turn-card">
    <div><span className="turn-index">{String(index + 1).padStart(2, '0')}</span><span className={`status ${turn.status}`}>{turn.status}</span><time>{stamp(turn.started_at)}</time></div>
    <p>{turn.prompt || 'Prompt content was not captured.'}</p>
    <small>{turn.kind} · {turn.ended_at ? `Ended ${stamp(turn.ended_at)}` : 'No completion captured'} · Duration {elapsed(duration)}</small>
    <button className="text-button prompt-toggle" aria-expanded={open} aria-controls={`prompt-${turn.id}`} onClick={() => setOpen(value => !value)}>{open ? 'Hide prompt activity ↑' : 'Inspect prompt activity ↓'}</button>
    {open && <section className="prompt-detail" id={`prompt-${turn.id}`} aria-label="Prompt activity">
      {error ? <p role="alert">{error} <button className="text-button" onClick={() => setRetry(value => value + 1)}>Retry</button></p> : !detail ? <p role="status">Loading prompt activity…</p> : <>
        <h3>{detail.calls.length} model calls · {detail.tools.length} tools</h3>
        <dl className="prompt-metrics">
          {tokenFields.map(([field, label]) => <div key={field}><dt>{label}</dt><dd>{count(sum(detail.calls.map(call => call[field])))}</dd></div>)}
          <div><dt>Cost reported</dt><dd>{cost(sum(detail.calls.map(call => call.cost_reported)))}</dd></div>
          <div><dt>Cost estimated</dt><dd>{cost(sum(detail.calls.filter(call => call.cost_reported == null).map(call => call.cost_estimated)))}</dd></div>
        </dl>
        <p className="prompt-coverage">Captured totals; missing fields are shown as —. {unpriced > 0 && `${unpriced} ${unpriced === 1 ? 'call has' : 'calls have'} no known price. `}Values represent API-equivalent usage; subscription billing can differ.</p>
        <h4>Model calls</h4>
        {detail.calls.length ? detail.calls.map(call => <CallCard key={call.id} call={call} />) : <p>No model usage was correlated with this prompt.</p>}
        <h4>Tools for this prompt</h4>
        {detail.tools.length ? detail.tools.map(tool => <ToolCard key={tool.id} tool={tool} />) : <p>No tools were correlated with this prompt.</p>}
      </>}
    </section>}
  </article>;
}
