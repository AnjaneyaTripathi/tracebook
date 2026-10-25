export interface Overview {
  calls: number; sessions: number; turns: number; tools: number; findings: number;
  input_uncached: number | null; input_cache_read: number | null; input_cache_write: number | null;
  output: number | null; reasoning: number | null; cost_reported: number | null;
  cost_estimated: number | null; unpriced_calls: number | null; parse_warnings: number;
  billing_mode: string;
  agents: { agent: string; calls: number; tokens: number; value: number | null }[];
  daily: { day: string; calls: number; tokens: number }[];
}
export interface Adapter {
  id: string; name: string; verification: string; capabilities: Record<string, 'yes' | 'no' | 'partial'>;
}
export interface Session {
  id: string; agent: string; native_id: string; project: string | null; started_at: string | null;
  turns: number; tools: number; tokens: number | null; first_prompt: string | null; resumed: number;
}
export interface Turn {
  id: string; prompt: string | null; started_at: string | null; ended_at: string | null; status: string; kind: string;
}
export interface Tool {
  id: string; tool: string; native_name: string; input: string | null; output: string | null;
  status: string; duration_ms: number | null; ts: string;
}
export interface Finding {
  id: string; detector: string; severity: string; agent: string; session_id: string;
  title: string; recommendation: string; evidence: string; ts: string;
}
export interface SessionDetail {
  id: string; agent: string; native_id: string; turns: Turn[]; tools: Tool[]; findings: Finding[];
}
export interface CanonicalEvent {
  id: string; kind: string; ts: string; data: Record<string, unknown>;
  provenance: Record<string, { source: string; ts: string; native_id: string }>;
}
export interface Report { id: string; window_start: string; window_end: string; created_at: string; body?: string }
