export type Screen = "A" | "B" | "C" | "D";

export interface Member {
  id: string;
  name: string;
  role: "parent" | "caregiver" | "child";
  age?: number | null;
  available: boolean;
  available_window?: string | null;
  needs_supervision?: boolean;
  ready_at?: string | null;
  note?: string | null;
}

export interface CalEvent {
  id: string;
  member_id: string;
  title: string;
  start: string;
  end: string;
  location: string;
  hard: boolean;
  kind: "commitment" | "duty";
  status: "confirmed" | "cancelled";
  responsibility_id?: string | null;
  new?: boolean;
}

export interface Escort {
  id: string;
  child_id: string;
  destination: string;
  window: string;
  target_arrival: string;
  assigned_member_id: string | null;
  depart: string | null;
  arrive: string | null;
}

export interface Segment {
  child_id: string;
  member_id: string;
  start: string;
  end: string;
}

export interface HouseholdState {
  household_id: string;
  name: string;
  date: string;
  state_version: number;
  members: Member[];
  calendar: CalEvent[];
  escorts: Escort[];
  supervision: Segment[];
  rules: string[];
}

export interface Conflict {
  code: string;
  detail: string;
  responsibility_id?: string | null;
  child_id?: string | null;
  member_id?: string | null;
  intervals: string[];
}

export interface Action {
  seq: number;
  action_id: string;
  type: string;
  summary: string;
  target: Record<string, unknown>;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
  status: string;
}

export interface Assignment {
  escort_id: string;
  child_id: string;
  member_id: string | null;
  depart: string | null;
  arrive: string | null;
}

export interface Unresolved {
  code: string;
  detail: string;
  child_id?: string | null;
  interval?: string | null;
  blockers: string[];
}

export interface Card {
  title: string;
  fields: string[];
  action?: string | null;
}

export interface Plan {
  plan_id: string;
  version: number;
  status: string;
  feasible: boolean;
  conflicts: Conflict[];
  actions: Action[];
  assignments: Assignment[];
  supervision: Segment[];
  rationale: string[];
  unresolved: Unresolved[];
  options: string[];
  objective: Record<string, number>;
  combinations_evaluated: number;
  changes_total?: number;
  people_notified?: string[];
  card?: Card | null;
}

export interface Check {
  seq: number;
  type: string;
  summary: string;
  verified: boolean;
}

export interface Verify {
  ready: boolean;
  checks: Check[];
  writes_verified: number;
  writes_total: number;
  affected_total: number;
  affected_resolved: number;
  unassigned: string[];
  violations: string[];
  failed_actions: string[];
  summary: string;
}

export interface ApplyResult {
  status: string;
  applied: number;
  failed: number;
  skipped_already_applied: number;
}

/** approve_recovery_plan's result, without its token. */
export interface Approval {
  plan_id: string;
  plan_version: number;
  decision: string;
  status: string;
  approved_actions?: number;
}

export interface WireEvent {
  method: string | null;
  tool?: string;
  status?: number;
  latency_ms?: number;
  at_ms?: number;
  protocol_header?: string | null;
  session?: string | null;
}

export interface Trace {
  protocol_version: string | null;
  tools: string[];
  wire: WireEvent[];
  calls: { tool: string; attempt: number; ok: boolean; error?: string; ms: number }[];
  intent: string | null;
  agent: string;
  model?: string;
  model_calls?: number;
  fallback?: string | null;
}

export interface View {
  session_id: string;
  agent: string;
  speaker?: string;
  screen: Screen;
  speech: string | null;
  transcript: { role: "user" | "assistant"; text: string; channel?: string }[];
  pending_notice: { raw: string; source: string; received_at: string } | null;
  notice: { structured: { delay_minutes: number | null; doors_open: string | null; classes_begin: string | null; confidence: number } } | null;
  conflicts: Conflict[];
  state: HouseholdState | null;
  plan: Plan | null;
  proposed_calendar: CalEvent[] | null;
  approval?: Approval | null;
  apply: ApplyResult | null;
  verify: Verify | null;
  app: McpApp | null;
  trace: Trace | null;
  degraded: boolean;
  error: { code: string; message: string } | null;
}

/** An MCP Apps view (ui:// resource, text/html;profile=mcp-app) and the tool result it renders. */
export interface McpApp {
  uri: string;
  html: string;
  tool: string;
  input?: Record<string, unknown>;
  result: { content: { type: string; text: string }[]; structuredContent: Record<string, unknown> };
}
