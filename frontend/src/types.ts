export interface Overview {
  calls: number; sessions: number; turns: number; tools: number; findings: number;
  input_uncached: number | null; input_cache_read: number | null; input_cache_write: number | null;
  output: number | null; reasoning: number | null; cost_reported: number | null;
  cost_estimated: number | null; unpriced_calls: number | null; parse_warnings: number;
  billing_mode: string; pricing_enabled: boolean;
  agents: { agent: string; calls: number; tokens: number; value: number | null }[];
  daily: { day: string; calls: number; tokens: number }[];
}
export interface Adapter {
  id: string; name: string; verification: string; capabilities: Record<string, 'yes' | 'no' | 'partial'>;
}
export interface Session {
  id: string; agent: string; native_id: string; project: string | null; started_at: string | null;
  turns: number; tools: number; tokens: number | null; first_prompt: string | null; resumed: number;
  models?: string | null; data?: string; cost?: number | null;
  is_subagent?: number; subagent_role?: string | null; calls?: number;
}
export interface SessionFacets {
  agents: { agent: string; sessions: number }[];
  projects: { id: string; root: string | null; sessions: number }[];
  models: { model: string; sessions: number }[];
  empty_sessions: number;
}
export interface Turn {
  id: string; prompt: string | null; started_at: string | null; ended_at: string | null; status: string; kind: string;
  input_uncached?: number; input_cache_read?: number; input_cache_write?: number;
  output?: number; reasoning?: number; cost?: number; models?: string | null;
}
export interface Tool {
  id: string; tool: string; native_name: string; input: string | null; output: string | null;
  status: string; duration_ms: number | null; ts: string; data?: string;
}
export interface ModelCall {
  id: string; model: string | null; data: string; ts: string;
  input_uncached: number | null; input_cache_read: number | null; input_cache_write: number | null;
  output: number | null; reasoning: number | null; total_reported: number | null;
  cost_reported: number | null; cost_estimated: number | null;
}
export interface TurnDetail extends Turn { calls: ModelCall[]; tools: Tool[] }
export interface Finding {
  id: string; detector: string; severity: string; agent: string; session_id: string;
  title: string; recommendation: string; evidence: string; ts: string;
}
export interface SubagentSummary {
  id: string; agent: string; native_id: string; started_at: string; data: string;
  turns: number; tools: number; tokens: number | null;
}
export interface SessionDetail {
  id: string; agent: string; native_id: string; turns: Turn[]; tools: Tool[]; findings: Finding[];
  data?: string; parent_session?: { id: string; agent: string; native_id: string; started_at: string } | null;
  subagents?: SubagentSummary[];
}
export interface CanonicalEvent {
  id: string; kind: string; ts: string; data: Record<string, unknown>;
  provenance: Record<string, { source: string; ts: string; native_id: string }>;
}
export interface Report { id: string; window_start: string; window_end: string; created_at: string; body?: string }
