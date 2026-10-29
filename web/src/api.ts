// Tiny fetch client for the heliosd API. All calls are same-origin; in dev the
// Vite proxy forwards /api to https://localhost:8420.
//
// Auth (since 2026-10-28, H1): the served page carries no token. The browser
// signs in once (signIn below) and heliosd sets an HttpOnly cookie that page
// code cannot read; every call sends X-Helios-Web: 1, without which the cookie
// does not count. A 401 means this browser is not signed in (or the token was
// rotated): the app shows its sign-in card.

import type {
  TodayResponse,
  MetricResponse,
  SleepResponse,
  ActivityResponse,
  ActionStatus,
  QuickLogProposal,
  QuickLogConfirm,
  QuickLogResult,
  LabParseResponse,
  LabCandidate,
  LabsResponse,
  WhoopPullResult,
  HealthResponse,
  ToolFreshness,
} from "./types";

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export function authHeaders(): Record<string, string> {
  return { "X-Helios-Web": "1" };
}

// Fired on any 401 so the app can show its sign-in card.
export const SIGN_IN_NEEDED = "helios:sign-in-needed";

function signInNeeded(): void {
  if (typeof window !== "undefined") window.dispatchEvent(new Event(SIGN_IN_NEEDED));
}

// Sign this browser in with the shared token. The token goes to the Mac once,
// in the body; it is never stored by the page. Resolves on success, throws an
// ApiError with a plain message otherwise.
export async function signIn(token: string): Promise<void> {
  let resp: Response;
  try {
    resp = await fetch("/api/session", {
      method: "POST",
      cache: "no-store",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({ token }),
    });
  } catch {
    throw new ApiError("Helios is offline.", 0);
  }
  if (resp.ok) return;
  if (resp.status === 401) throw new ApiError("That token is not right.", 401);
  if (resp.status === 429) throw new ApiError("Too many tries. Wait a minute, then try again.", 429);
  throw new ApiError(describeFailure(resp.status), resp.status);
}

function describeFailure(status: number): string {
  if (status === 401) {
    return "This browser is not signed in to Helios. Sign in with the token from the Mac.";
  }
  if (status === 403) {
    return "Helios refused this device (403). Only the owner's own devices may reach it.";
  }
  if (status >= 500) {
    return `Helios hit a server error (${status}). The data on the Mac is unchanged; try again in a moment.`;
  }
  return `Request failed (${status}).`;
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let resp: Response;
  try {
    resp = await fetch(path, {
      // Never let the browser serve a cached API response; the server also
      // sends Cache-Control: no-store. Together this stops stale dashboard data.
      cache: "no-store",
      ...init,
      headers: { "Content-Type": "application/json", ...authHeaders(), ...(init?.headers ?? {}) },
    });
  } catch {
    // Network error: backend offline.
    throw new ApiError("Helios is offline.", 0);
  }
  if (!resp.ok) {
    if (resp.status === 401) signInNeeded();
    throw new ApiError(describeFailure(resp.status), resp.status);
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

  setActionStatus: (actionId: string, status: ActionStatus) =>
    request<unknown>(`/api/actions/${encodeURIComponent(actionId)}/${status}`, {
      method: "POST",
    }),

  recompute: (days = 7) =>
    request<unknown>(`/api/recompute?days=${days}`, { method: "POST" }),

  // Ask the Mac to fetch the latest Whoop records (last night's sleep and
  // recovery once Whoop has scored them). The server rate-limits it.
  whoopPull: (days = 3) =>
    request<WhoopPullResult>(`/api/whoop/pull?days=${days}`, { method: "POST" }),

  freshness: () => request<unknown>("/api/freshness"),

  // System screen (rank 4). /api/tool/freshness runs the watchdog in a
  // worker thread; /api/freshness groups every sample on the event loop, so
  // the screen does not use it. /api/health's one COUNT(*) still runs on the
  // loop (a recorded rank 2 proposal moves it off).
  health: () => request<HealthResponse>("/api/health"),
  systemStatus: () => request<ToolFreshness>("/api/tool/freshness"),

  // The frozen screens carry their own reads (rank 4), so the built app has
  // none of them: Ask Helios's chat and free-text parse (screens/Chat.tsx),
  // the Insights report (screens/Insights.tsx), the 7-day Actions log
  // (screens/Actions.tsx).

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
      resp = await fetch("/api/labs/parse", { method: "POST", body: form, headers: authHeaders() });
    } catch {
      throw new ApiError("Helios is offline.", 0);
    }
    if (resp.status === 401) {
      signInNeeded();
      throw new ApiError(describeFailure(401), 401);
    }
    if (resp.status === 413) throw new ApiError("That file is too large (limit 25 MB).", 413);
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
