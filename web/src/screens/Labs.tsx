import { useMemo, useRef, useState } from "react";
import { api } from "../api";
import { useAsync } from "../lib/useAsync";
import type { LabCandidate, LabRecord } from "../types";
import { Card, SectionTitle } from "../components/Card";
import { EmptyState, LoadingState, OfflineState } from "../components/states";

type RangeLike = { value: number; ref_low: number | null; ref_high: number | null };

function flagOf(r: RangeLike): "low" | "high" | null {
  if (r.ref_low != null && r.value < r.ref_low) return "low";
  if (r.ref_high != null && r.value > r.ref_high) return "high";
  return null;
}

function refText(lo: number | null, hi: number | null): string {
  if (lo != null && hi != null) return `${lo} – ${hi}`;
  if (hi != null) return `< ${hi}`;
  if (lo != null) return `> ${lo}`;
  return "";
}

function confColor(c: number): string {
  if (c >= 0.7) return "var(--mint)";
  if (c >= 0.4) return "#d8b24a";
  return "#c96b6b";
}

export function Labs() {
  const { data, loading, offline, reload } = useAsync(() => api.labs(), [], "labs");
  const fileRef = useRef<HTMLInputElement>(null);

  const [rows, setRows] = useState<LabCandidate[] | null>(null);
  const [include, setInclude] = useState<boolean[]>([]);
  const [panelDate, setPanelDate] = useState("");
  const [source, setSource] = useState("");
  const [parsing, setParsing] = useState(false);
  const [parseMsg, setParseMsg] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [savedMsg, setSavedMsg] = useState<string | null>(null);

  async function onFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    setParsing(true);
    setParseMsg(null);
    setSavedMsg(null);
    setRows(null);
    try {
      const res = await api.labsParse(file);
      if (res.error) {
        setParseMsg(res.error);
      } else if (res.needs_ocr) {
        setParseMsg(
          "That looks like an image. On-device OCR is not installed yet, so image reports cannot be read. A PDF export works today."
        );
      } else {
        const cands = res.candidates ?? [];
        setRows(cands);
        setInclude(cands.map(() => true));
        setPanelDate(res.panel_date ?? new Date().toISOString().slice(0, 10));
        setSource(res.filename ?? "lab report");
        if (cands.length === 0) {
          setParseMsg(
            "No biomarkers were recognized in that file. Try a clearer PDF export, or a report that lists values as text rather than a scan."
          );
        }
      }
    } catch (err) {
      setParseMsg(err instanceof Error ? err.message : "Could not read that file.");
    } finally {
      setParsing(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  function edit(i: number, patch: Partial<LabCandidate>) {
    setRows((rs) => (rs ? rs.map((r, j) => (j === i ? { ...r, ...patch } : r)) : rs));
  }
  function toggle(i: number) {
    setInclude((inc) => inc.map((v, j) => (j === i ? !v : v)));
  }

  const chosenCount = include.filter(Boolean).length;

  async function confirm() {
    if (!rows) return;
    const chosen = rows.filter((_, i) => include[i]);
    if (chosen.length === 0) return;
    setSaving(true);
    setSavedMsg(null);
    try {
      const res = await api.labsConfirm(panelDate, chosen, source);
      setSavedMsg(`Stored ${res.stored} value${res.stored === 1 ? "" : "s"} from ${panelDate}.`);
      setRows(null);
      setInclude([]);
      reload();
    } catch (err) {
      setSavedMsg(err instanceof Error ? err.message : "Could not store those values.");
    } finally {
      setSaving(false);
    }
  }

  // History grouped by panel date, newest first.
  const panels = useMemo(() => {
    const byDate = new Map<string, LabRecord[]>();
    for (const r of data?.labs ?? []) {
      const list = byDate.get(r.panel_date) ?? [];
      list.push(r);
      byDate.set(r.panel_date, list);
    }
    return Array.from(byDate.entries()).sort((a, b) => (a[0] < b[0] ? 1 : -1));
  }, [data]);

  if (offline) return <OfflineState onRetry={reload} />;

  return (
    <div className="space-y-6 animate-fade">
      <header>
        <h1 className="font-serif text-3xl leading-tight">Labs</h1>
        <p className="mt-2 text-sm leading-relaxed text-muted">
          Import bloodwork from a PDF. Every value is shown for you to confirm before it
          is stored, and the file never leaves this Mac.
        </p>
      </header>

      <section>
        <input
          ref={fileRef}
          type="file"
          accept=".pdf,.txt,image/*"
          onChange={onFile}
          className="hidden"
        />
        <button
          onClick={() => fileRef.current?.click()}
          disabled={parsing}
          className="w-full rounded-2xl border border-dashed border-hairline bg-surface px-4 py-6 text-sm transition-colors hover:bg-hairline/30 disabled:opacity-60"
        >
          {parsing ? "Reading the report..." : "Upload a lab report (PDF)"}
        </button>
        {parseMsg ? (
          <p className="mt-3 text-sm leading-relaxed text-muted">{parseMsg}</p>
        ) : null}
        {savedMsg ? (
          <p className="mt-3 text-sm leading-relaxed text-mint">{savedMsg}</p>
        ) : null}
      </section>

      {rows && rows.length > 0 ? (
        <section>
          <SectionTitle>Confirm values</SectionTitle>
          <Card>
            <div className="mb-4 flex items-center justify-between gap-3">
              <label className="text-sm text-muted">
                Panel date
                <input
                  type="date"
                  value={panelDate}
                  onChange={(e) => setPanelDate(e.target.value)}
                  className="ml-2 rounded-lg border border-hairline bg-bg px-2 py-1 text-sm text-text"
                />
              </label>
              <span className="text-xs text-muted">{chosenCount} selected</span>
            </div>

            <div className="space-y-3">
              {rows.map((r, i) => {
                const fl = flagOf(r);
                return (
                  <div
                    key={`${r.biomarker}-${i}`}
                    className="flex items-center gap-3 border-t border-hairline pt-3 first:border-t-0 first:pt-0"
                  >
                    <input
                      type="checkbox"
                      checked={include[i] ?? false}
                      onChange={() => toggle(i)}
                      className="h-4 w-4 shrink-0 accent-[color:var(--mint)]"
                    />
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <span className="truncate text-sm">{r.biomarker}</span>
                        {r.confidence != null ? (
                          <span
                            className="inline-block h-1.5 w-1.5 shrink-0 rounded-full"
                            style={{ backgroundColor: confColor(r.confidence) }}
                            title={`extraction confidence ${r.confidence}`}
                          />
                        ) : null}
                      </div>
                      {r.ref_low != null || r.ref_high != null ? (
                        <p className="text-[11px] text-muted">
                          ref {refText(r.ref_low, r.ref_high)}
                          {fl ? (
                            <span style={{ color: fl === "high" ? "#c96b6b" : "#d8b24a" }}>
                              {"  "}
                              {fl === "high" ? "above range" : "below range"}
                            </span>
                          ) : null}
                        </p>
                      ) : null}
                    </div>
                    <input
                      type="number"
                      step="any"
                      value={r.value}
                      onChange={(e) => edit(i, { value: parseFloat(e.target.value) })}
                      className="w-20 rounded-lg border border-hairline bg-bg px-2 py-1 text-right text-sm tnum text-text"
                    />
                    <input
                      type="text"
                      value={r.unit ?? ""}
                      placeholder="unit"
                      onChange={(e) => edit(i, { unit: e.target.value })}
                      className="w-20 rounded-lg border border-hairline bg-bg px-2 py-1 text-sm text-muted"
                    />
                  </div>
                );
              })}
            </div>

            <button
              onClick={confirm}
              disabled={saving || chosenCount === 0}
              className="mt-5 w-full rounded-full px-4 py-2.5 text-sm font-medium transition-transform active:scale-[0.99] disabled:opacity-50"
              style={{ backgroundColor: "var(--mint)", color: "var(--bg)" }}
            >
              {saving ? "Storing..." : `Confirm and store ${chosenCount} value${chosenCount === 1 ? "" : "s"}`}
            </button>
            <p className="mt-3 text-xs leading-relaxed text-muted">
              Values are stored exactly as confirmed. Nothing is auto-accepted, and the
              extractor never diagnoses.
            </p>
          </Card>
        </section>
      ) : null}

      <section>
        <SectionTitle>History</SectionTitle>
        {loading && !data ? (
          <LoadingState label="Reading your labs" />
        ) : panels.length === 0 ? (
          <EmptyState
            title="No labs yet."
            body="Upload a bloodwork PDF above to start a personal history. Once stored, biomarkers can be tracked over time and folded into insights."
          />
        ) : (
          <div className="space-y-4">
            {panels.map(([pdate, recs]) => (
              <Card key={pdate}>
                <p className="mb-3 font-serif text-lg">{pdate}</p>
                <div>
                  {recs.map((r) => {
                    const fl = flagOf(r);
                    return (
                      <div
                        key={r.lab_id}
                        className="flex items-baseline justify-between gap-4 border-t border-hairline py-2 first:border-t-0 first:pt-0"
                      >
                        <span className="text-sm text-muted">{r.biomarker}</span>
                        <span className="flex items-baseline gap-2">
                          <span
                            className="font-serif text-lg tnum"
                            style={{
                              color: fl === "high" ? "#c96b6b" : fl === "low" ? "#d8b24a" : "var(--text)",
                            }}
                          >
                            {r.value}
                          </span>
                          {r.unit ? <span className="text-xs text-muted">{r.unit}</span> : null}
                          {r.ref_low != null || r.ref_high != null ? (
                            <span className="text-[11px] text-muted">
                              ({refText(r.ref_low, r.ref_high)})
                            </span>
                          ) : null}
                        </span>
                      </div>
                    );
                  })}
                </div>
              </Card>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
