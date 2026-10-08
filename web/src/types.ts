// Shared shapes for the heliosd JSON API. Kept permissive where the backend
// may omit fields so the UI never crashes on partial data.

// in_progress: a running total on the reporting today (shown "so far", no
// flag, no delta, no grade until the day closes; owner decision D7).
// fallback: the value comes from a device that is not the metric's owner, so
// there is no like-for-like delta against the owner's baseline (A6).
export type SignalState =
  | "favorable"
  | "neutral"
  | "flag"
  | "insufficient"
  | "in_progress"
  | "fallback";
export type Grade = "A" | "B" | "C" | "D";

export interface Signal {
  // The day the signal describes (YYYY-MM-DD, reporting zone).
  date?: string;
  metric: string;
  state: SignalState;
  value: number | null;
  unit: string;
  baseline_median?: number | null;
  baseline_mad?: number | null;
  delta_pct?: number | null;
  device_key?: string;
  confidence?: number;
  grade?: Grade | null;
  context_flags?: string[];
  why?: string;
  // True when the day's value is from a non-owner device (see SignalState).
  fallback?: boolean;
  // True for a running total on the reporting today.
  partial?: boolean;
}

export interface ActionItem {
  action_id?: string;
  text: string;
  category?: string;
  // Stored resolution from the actions table. /api/today sends it, so a
  // reload or another device shows Adopted or Dismissed, not the buttons.
  status?: ActionStatus;
}

export interface FocusItem {
  name: string;
  // null when there is no value yet for the day (never a fake 0).
  current: number | null;
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
  // "ready": a validated local-AI narrative is shown. "generating": the shown
  // narrative is the instant deterministic template while the model writes a
  // richer one in the background. "template": model unavailable, template final.
  narrative_status?: "ready" | "generating" | "template";
  // Last time the phone delivered a batch to the Mac. Shown on Today so a
  // lagging number reads as lag, not breakage. Offset-aware ISO 8601 from the
  // Wave 1 server; a naive reporting-zone wall time from older builds.
  as_of?: string;
  // IANA name of the reporting zone (the calendar every date here is in).
  zone?: string;
  // Core markers not yet in for the day (for example recovery_score and
  // hrv_rmssd before Whoop has scored the night). Non-empty means the verdict
  // is a waiting state, not a recovery judgement.
  awaiting?: string[];
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
  // The server's reporting-zone today (YYYY-MM-DD), when the server sends it.
  reporting_date?: string;
}

export interface SleepStages {
  deep_min: number;
  rem_min: number;
  light_min: number;
  awake_min: number;
}

export interface SleepNight {
  date: string;
  asleep_h: number | null;
  device?: string;
  grade?: Grade;
  in_bed_h?: number;
  efficiency_pct?: number;
  stages?: SleepStages;
  stage_source?: string;
  fell_asleep?: string | null;
  woke?: string | null;
}

export interface SleepSummary {
  last_night?: SleepNight | null;
  avg_7d?: number | null;
  avg_prev_7d?: number | null;
  // The night one week before the last night (keyed on the last night's
  // date, not on today).
  same_weekday_last_week?: number | null;
  median?: number | null;
  efficiency_avg_7d?: number | null;
  // How many nights the averages really cover, when the server says.
  n_7d?: number | null;
  efficiency_n_7d?: number | null;
  same_weekday_last_week_date?: string | null;
}

export interface SleepResponse {
  nights: SleepNight[];
  summary: SleepSummary;
  reporting_date?: string;
}

export interface ActivityPoint {
  date: string;
  value: number;
  device_key?: string;
  grade?: Grade | null;
  partial?: boolean;
}

export interface ActivityResponse {
  steps: ActivityPoint[];
  active_energy: ActivityPoint[];
  strain: ActivityPoint[];
  vo2max: ActivityPoint[];
  reporting_date?: string;
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

// POST /api/whoop/pull. ok with counts, or skipped: "rate_limited" with the
// seconds until the next pull is allowed. Extra keys are counts.
export interface WhoopPullResult {
  ok?: boolean;
  skipped?: string;
  retry_after_s?: number;
  error?: string;
  [key: string]: unknown;
}

export interface ActionsResponse {
  actions: ActionHistoryItem[];
  reporting_date?: string;
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
  kind?: string;
  item?: string;
}

export interface QuickLogResult extends QuickLogConfirm {
  ts?: string;
  parser?: "llm" | "rules";
  summary?: string;
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

export interface LabCandidate {
  biomarker: string;
  value: number;
  unit: string | null;
  ref_low: number | null;
  ref_high: number | null;
  confidence?: number;
}

export interface LabParseResponse {
  panel_date: string | null;
  candidates: LabCandidate[];
  needs_ocr?: boolean;
  error?: string;
  filename?: string;
}

export interface LabRecord {
  lab_id: string;
  panel_date: string;
  biomarker: string;
  value: number;
  unit: string | null;
  ref_low: number | null;
  ref_high: number | null;
  panel_source?: string;
}

export interface LabsResponse {
  labs: LabRecord[];
}
