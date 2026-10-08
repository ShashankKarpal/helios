import { humanizeDevice, gradeColorVar } from "../lib/format";
import type { Grade } from "../types";

interface Props {
  deviceKey?: string;
  grade?: Grade | null;
  // The value comes from a device that is not the metric's owner.
  fallback?: boolean;
  // A running total on the reporting today: no grade until the day closes.
  partial?: boolean;
  // Overrides the device name, for a value more than one device fed
  // ("Watch + iPhone" on a merged steps day, owner decision D4).
  label?: string | null;
}

const GRADE_WORDS: Record<Grade, string> = {
  A: "grade A, fully corroborated",
  B: "grade B, good",
  C: "grade C, thin or disputed",
  D: "grade D, weak",
};

// Small pill showing where a reading came from and its data grade. The grade
// is a letter with a spelled-out title, never colour alone; a fallback device
// and a day still in progress are said in words.
export function ProvenanceChip({ deviceKey, grade, fallback, partial, label }: Props) {
  const showGrade = grade && !partial;
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full border border-hairline bg-bg/60 px-2.5 py-1 text-xs text-muted">
      <span
        className="inline-block h-1.5 w-1.5 rounded-full"
        style={{ backgroundColor: "var(--muted)" }}
      />
      <span className="text-text/80">{label || humanizeDevice(deviceKey)}</span>
      {fallback ? <span className="text-muted">fallback</span> : null}
      {partial ? <span className="text-muted">so far</span> : null}
      {showGrade ? (
        <span
          className="ml-0.5 font-semibold tnum"
          style={{ color: gradeColorVar(grade) }}
          title={GRADE_WORDS[grade]}
          aria-label={GRADE_WORDS[grade]}
        >
          {grade}
        </span>
      ) : null}
    </span>
  );
}
