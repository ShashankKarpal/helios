// Tiny fetch client for the heliosd API. All calls are same-origin; in dev the
// Vite proxy forwards /api to http://localhost:8420. No storage, no auth.

import type {
  TodayResponse,
  MetricResponse,
  SleepResponse,
  ActivityResponse,
  ActionsResponse,
  ActionStatus,
  ChatResponse,
  QuickLogProposal,
  QuickLogConfirm,
  QuickLogResult,
  InsightsResponse,
  LabParseResponse,
  LabCandidate,
  LabsResponse,
} from "./types";

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let resp: Response;
  try {
    resp = await fetch(path, {
      // Never let the browser serve a cached API response; the server also
      // sends Cache-Control: no-store. Together this stops stale dashboard data.
      cache: "no-store",
      headers: { "Content-Type": "application/json" },
      ...init,
    });
  } catch {
    // Network error: backend offline.
    throw new ApiError("Helios is offline.", 0);
  }
  if (!resp.ok) {
    throw new ApiError(`Request failed (${resp.status}).`, resp.status);
  }
  // Some POSTs may return empty bodies.
  const text = await resp.text();
  return (text ? JSON.parse(text) : {}) as T;
}

export const api = {
  today: () => request<TodayResponse>("/api/today"),

  metric: (metric: string, days = 30) =>
    request<MetricResponse>(`/api/metrics/${encodeURIComponent(metric)}?days=${days}`),

  sleep: (days = 30) => request<SleepResponse>(`/api/sleep?days=${days}`),

  activity: (days = 30) => request<ActivityResponse>(`/api/activity?days=${days}`),

  actions: (days = 7) => request<ActionsResponse>(`/api/actions?days=${days}`),

  setActionStatus: (actionId: string, status: ActionStatus) =>
    request<unknown>(`/api/actions/${encodeURIComponent(actionId)}/${status}`, {
      method: "POST",
    }),

  recompute: (days = 7) =>
    request<unknown>(`/api/recompute?days=${days}`, { method: "POST" }),

  freshness: () => request<unknown>("/api/freshness"),

  insights: (days = 90) => request<InsightsResponse>(`/api/insights?days=${days}`),

  chat: (message: string, sessionId?: string) =>
    request<ChatResponse>("/api/chat", {
      method: "POST",
      body: JSON.stringify({ message, session_id: sessionId }),
    }),

  quicklog: (text: string) =>
    request<QuickLogProposal>("/api/quicklog", {
      method: "POST",
      body: JSON.stringify({ text }),
    }),

  quicklogConfirm: (proposal: QuickLogProposal, source = "pwa") =>
    request<QuickLogConfirm>("/api/quicklog/confirm", {
      method: "POST",
      body: JSON.stringify({
        kind: proposal.kind,
        item: proposal.item,
        amount: proposal.amount,
        minutes_ago: proposal.minutes_ago,
        source,
      }),
    }),

  // One-shot capture: parse + store in a single call, no confirm step.
  quicklogLog: (text: string, source = "pwa") =>
    request<QuickLogResult>("/api/quicklog/log", {
      method: "POST",
      body: JSON.stringify({ text, source }),
    }),

  labs: () => request<LabsResponse>("/api/labs"),

  // Multipart upload: let the browser set the boundary, so this bypasses the
  // JSON request() helper. The file never leaves the Mac; parsing is local.
  labsParse: async (file: File): Promise<LabParseResponse> => {
    const form = new FormData();
    form.append("file", file);
    let resp: Response;
    try {
      resp = await fetch("/api/labs/parse", { method: "POST", body: form });
    } catch {
      throw new ApiError("Helios is offline.", 0);
    }
    if (!resp.ok) throw new ApiError(`Could not read that file (${resp.status}).`, resp.status);
    return (await resp.json()) as LabParseResponse;
  },

  labsConfirm: (panelDate: string, rows: LabCandidate[], panelSource?: string) =>
    request<{ stored: number; lab_ids: string[]; panel_date: string }>(
      "/api/labs/confirm",
      {
        method: "POST",
        body: JSON.stringify({ panel_date: panelDate, rows, panel_source: panelSource }),
      }
    ),
};
