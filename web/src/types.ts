// Shared shapes for the heliosd JSON API. Kept permissive where the backend
// may omit fields so the UI never crashes on partial data.

export type SignalState = "favorable" | "neutral" | "flag" | "insufficient";
export type Grade = "A" | "B" | "C" | "D";

export interface Signal {
  metric: string;
  state: SignalState;
  value: number | null;
  unit: string;
  baseline_median?: number | null;
  baseline_mad?: number | null;
  delta_pct?: number | null;
  device_key?: string;
  confidence?: number;
  grade?: Grade;
  context_flags?: string[];
  why?: string;
}

export interface ActionItem {
  action_id?: string;
  text: string;
  category?: string;
}

export interface FocusItem {
  name: string;
  current: number;
  target: number;
  unit: string;
}

export interface TodayResponse {
  date: string;
  greeting: string;
  verdict: string;
  narrative: string;
  signals: Signal[];
  actions: ActionItem[];
  context_flags: string[];
  focus: FocusItem[];
  model?: string;
  validated?: boolean;
}

export interface MetricPoint {
  date: string;
  value: number;
  unit?: string;
  device_key?: string;
  grade?: Grade;
  confidence?: number;
  corroboration?: number;
}

export interface Baseline {
  window_days: number;
  median: number;
  mad: number;
}

export interface MetricResponse {
  metric: string;
  series: MetricPoint[];
  baselines: Baseline[];
}

export interface SleepStage {
  date: string;
  device_key?: string;
  stage: string;
  minutes: number;
}

export interface SleepDuration {
  date: string;
  value: number;
  device_key?: string;
  grade?: Grade;
}

export interface SleepResponse {
  stages: SleepStage[];
  durations: SleepDuration[];
}

export interface ActivityPoint {
  date: string;
  value: number;
  device_key?: string;
  grade?: Grade;
}

export interface ActivityResponse {
  steps: ActivityPoint[];
  active_energy: ActivityPoint[];
  strain: ActivityPoint[];
  vo2max: ActivityPoint[];
}

export type ActionStatus = "adopted" | "dismissed" | "done" | "suggested";

export interface ActionHistoryItem {
  action_id: string;
  date: string;
  text: string;
  category?: string;
  status: ActionStatus;
  created_by?: string;
}

export interface ActionsResponse {
  actions: ActionHistoryItem[];
}

export interface Citation {
  metric: string;
  value: string | number;
  date_range?: string;
  device?: string;
  confidence?: number;
}

export interface ChatResponse {
  answer: string;
  citations: Citation[];
  caveats: string[];
  followups: string[];
  session_id?: string;
  tool_calls?: string[];
}

export interface QuickLogProposal {
  kind: string;
  item: string;
  amount: string | number;
  minutes_ago: number;
  raw_text?: string;
}

export interface QuickLogConfirm {
  stored: boolean;
  event_id?: string;
}

export interface Insight {
  title: string;
  detail: string;
  method: string;
  verdict: string;
}

export interface InsightsResponse {
  insights: Insight[];
}
