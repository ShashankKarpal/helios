// The app's navigation (rank 4, 2026-10-29): three tabs. Today is the daily
// answer, History holds the record screens, System says whether the pipeline
// is healthy. Ask Helios, Insights and the Actions log are frozen (owner
// decisions 2026-10-21 and 2026-10-29): their files stay in src/screens but
// nothing imports them, so they are not in the built app. Rank 5 added
// Workouts and ECG to History; rank 6 reworks Labs. Checked by
// scripts/check-nav.mjs.

export type TabKey = "today" | "history" | "system";

export interface Tab {
  key: TabKey;
  label: string;
  icon: string;
}

export const TABS: readonly Tab[] = [
  { key: "today", label: "Today", icon: "sun" },
  { key: "history", label: "History", icon: "history" },
  { key: "system", label: "System", icon: "system" },
];

export const DEFAULT_TAB: TabKey = "today";

export type HistoryKey = "sleep" | "activity" | "workouts" | "labs";

export interface Segment {
  key: HistoryKey;
  label: string;
}

export const HISTORY_SEGMENTS: readonly Segment[] = [
  { key: "sleep", label: "Sleep" },
  { key: "activity", label: "Activity" },
  { key: "workouts", label: "Workouts" },
  { key: "labs", label: "Labs" },
];

export const DEFAULT_SEGMENT: HistoryKey = "sleep";
