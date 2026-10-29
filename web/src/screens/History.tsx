import { useState } from "react";
import { DEFAULT_SEGMENT, HISTORY_SEGMENTS } from "../lib/nav";
import type { HistoryKey } from "../lib/nav";
import { Sleep } from "./Sleep";
import { Activity } from "./Activity";
import { Labs } from "./Labs";

// The record screens under one tab (rank 4). Each segment is the screen it
// was before, title and all; only the way in changed. Rank 5 adds Workouts
// and ECG here.
export function History() {
  const [segment, setSegment] = useState<HistoryKey>(DEFAULT_SEGMENT);

  return (
    <div className="space-y-6">
      <div
        role="tablist"
        aria-label="History sections"
        className="flex gap-1 rounded-full border border-hairline bg-surface p-1"
      >
        {HISTORY_SEGMENTS.map((s) => {
          const active = s.key === segment;
          return (
            <button
              key={s.key}
              role="tab"
              aria-selected={active}
              onClick={() => setSegment(s.key)}
              className={`flex-1 rounded-full px-3 py-1.5 text-xs tracking-wide transition-colors ${
                active ? "bg-hairline/60" : "hover:bg-hairline/30"
              }`}
              style={{ color: active ? "var(--mint)" : "var(--muted)" }}
            >
              {s.label}
            </button>
          );
        })}
      </div>

      {segment === "sleep" && <Sleep />}
      {segment === "activity" && <Activity />}
      {segment === "labs" && <Labs />}
    </div>
  );
}
