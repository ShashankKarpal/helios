import { humanizeDevice, gradeColorVar } from "../lib/format";
import type { Grade } from "../types";

interface Props {
  deviceKey?: string;
  grade?: Grade;
}

// Small pill showing where a reading came from and its data grade.
export function ProvenanceChip({ deviceKey, grade }: Props) {
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full border border-hairline bg-bg/60 px-2.5 py-1 text-[11px] text-muted">
      <span
        className="inline-block h-1.5 w-1.5 rounded-full"
        style={{ backgroundColor: "var(--muted)" }}
      />
      <span className="text-text/80">{humanizeDevice(deviceKey)}</span>
      {grade ? (
        <span
          className="ml-0.5 font-semibold tnum"
          style={{ color: gradeColorVar(grade) }}
        >
          {grade}
        </span>
      ) : null}
    </span>
  );
}
