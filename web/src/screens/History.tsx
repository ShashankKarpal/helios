import { useState } from "react";
import { DEFAULT_SEGMENT, HISTORY_SEGMENTS } from "../lib/nav";
import type { HistoryKey } from "../lib/nav";
import { Sleep } from "./Sleep";
import { Activity } from "./Activity";
import { Labs } from "./Labs";
import { Workouts } from "./Workouts";
import { Ecg } from "./Ecg";

// The record screens under one tab (rank 4). Each segment is the screen it
// was before, title and all; only the way in changed. Rank 5 added Workouts
// and ECG: five segments, so each button takes its own width plus a share
// of the rest (flex-auto) and fits at 320 px.
//
// The row is a group of toggle buttons (aria-pressed), not a tab list: a tab
// list promises arrow keys and a linked panel (review finding 3). The chosen
// segment outlives a tab switch for this page load (finding 11), like the
// data cache in useAsync.
let remembered: HistoryKey = DEFAULT_SEGMENT;

export function History() {
  const [segment, setSegment] = useState<HistoryKey>(remembered);

  function choose(key: HistoryKey) {
    remembered = key;
    setSegment(key);
  }

  return (
    <div className="space-y-6">
      <div
        role="group"
        aria-label="History sections"
        className="flex gap-1 rounded-full border border-hairline bg-surface p-1"
      >
        {HISTORY_SEGMENTS.map((s) => {
          const active = s.key === segment;
          return (
            <button
              key={s.key}
              aria-pressed={active}
              onClick={() => choose(s.key)}
              className={`flex-auto whitespace-nowrap rounded-full px-1.5 py-1.5 text-xs tracking-wide transition-colors ${
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
      {segment === "workouts" && <Workouts />}
      {segment === "ecg" && <Ecg />}
      {segment === "labs" && <Labs />}
    </div>
  );
}
