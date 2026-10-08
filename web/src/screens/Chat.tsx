import { useState, useRef, useEffect } from "react";
import { api, ApiError } from "../api";
import { humanizeDevice, humanizeMetric } from "../lib/format";
import type { Citation, QuickLogProposal } from "../types";

interface Message {
  id: string;
  role: "user" | "assistant";
  text: string;
  citations?: Citation[];
  caveats?: string[];
  followups?: string[];
}

let counter = 0;
function nextId() {
  counter += 1;
  return `m${counter}`;
}

function CitationChip({ c }: { c: Citation }) {
  const conf =
    c.confidence != null ? ` conf ${Math.round(c.confidence * 100)}%` : "";
  const device = c.device ? `, ${humanizeDevice(c.device)}` : "";
  return (
    <span className="rounded-full border border-hairline bg-bg/60 px-2.5 py-1 text-xs text-muted">
      <span className="text-text/80">{humanizeMetric(c.metric)}</span>: {String(c.value)}
      <span className="text-muted">
        {" "}
        ({device.replace(/^, /, "")}
        {conf})
      </span>
    </span>
  );
}

function QuickLog() {
  const [text, setText] = useState("");
  const [proposal, setProposal] = useState<QuickLogProposal | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  async function parse() {
    if (!text.trim()) return;
    setBusy(true);
    setNote(null);
    try {
      const p = await api.quicklog(text.trim());
      setProposal(p);
    } catch (err) {
      setNote(
        err instanceof ApiError && err.status === 0
          ? "Helios is offline. Cannot log right now."
          : "Could not parse that. Try rephrasing."
      );
    } finally {
      setBusy(false);
    }
  }

  async function confirm() {
    if (!proposal) return;
    setBusy(true);
    try {
      const res = await api.quicklogConfirm(proposal);
      setNote(res.stored ? "Logged." : "Not stored.");
      setProposal(null);
      setText("");
    } catch {
      setNote("Could not save that entry.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="rounded-2xl border border-hairline bg-surface p-3">
      <p className="mb-2 text-xs uppercase tracking-wide text-muted">
        Quick log
      </p>
      <div className="flex items-center gap-2">
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") parse();
          }}
          placeholder="e.g. coffee 20 min ago"
          className="min-w-0 flex-1 rounded-full border border-hairline bg-bg px-3 py-2 text-sm text-text outline-none placeholder:text-muted focus:border-mint/50"
        />
        <button
          onClick={parse}
          disabled={busy || !text.trim()}
          className="shrink-0 rounded-full border border-hairline px-3 py-2 text-xs text-text transition-colors hover:bg-hairline/40 disabled:opacity-40"
        >
          Parse
        </button>
      </div>
      {proposal ? (
        <div className="mt-3 rounded-xl border border-hairline bg-bg/60 p-3 text-sm">
          <p className="text-muted">
            Log{" "}
            <span className="text-text">
              {String(proposal.amount)} {proposal.item}
            </span>{" "}
            <span className="text-muted">({proposal.kind})</span>,{" "}
            {proposal.minutes_ago} min ago?
          </p>
          <div className="mt-2 flex gap-2">
            <button
              onClick={confirm}
              disabled={busy}
              className="rounded-full px-3 py-1 text-xs font-medium disabled:opacity-40"
              style={{ color: "var(--mint)", border: "1px solid var(--mint)" }}
            >
              Confirm
            </button>
            <button
              onClick={() => setProposal(null)}
              className="rounded-full border border-hairline px-3 py-1 text-xs text-muted"
            >
              Cancel
            </button>
          </div>
        </div>
      ) : null}
      {note ? <p className="mt-2 text-xs text-muted">{note}</p> : null}
    </div>
  );
}

// The server session id and the visible transcript live in browser storage
// (per viewer, this browser only): switching tabs unmounts this screen, and
// before this every return started a fresh server session with no memory of
// the earlier turns. heliosd has no endpoint yet that lists a session's
// stored messages, so the transcript shown is the one this browser saw.
const SESSION_KEY = "helios.chat.session";
const TRANSCRIPT_KEY = "helios.chat.transcript";

function readStored<T>(key: string, storage: Storage | undefined): T | null {
  try {
    const raw = storage?.getItem(key);
    return raw ? (JSON.parse(raw) as T) : null;
  } catch {
    return null;
  }
}

function writeStored(key: string, storage: Storage | undefined, value: unknown): void {
  try {
    if (value == null) storage?.removeItem(key);
    else storage?.setItem(key, JSON.stringify(value));
  } catch {
    // storage may be unavailable (private mode, cleared site data): the chat still works.
  }
}

function storages(): { local: Storage | undefined; session: Storage | undefined } {
  try {
    return { local: window.localStorage, session: window.sessionStorage };
  } catch {
    return { local: undefined, session: undefined };
  }
}

export function Chat() {
  const { local, session } = storages();
  const [messages, setMessages] = useState<Message[]>(
    () => readStored<Message[]>(TRANSCRIPT_KEY, session) ?? []
  );
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [sessionId, setSessionId] = useState<string | undefined>(
    () => readStored<string>(SESSION_KEY, local) ?? undefined
  );
  const resumed = useRef(sessionId != null);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    writeStored(TRANSCRIPT_KEY, session, messages.length ? messages.slice(-40) : null);
  }, [messages, session]);

  function startNew() {
    setSessionId(undefined);
    setMessages([]);
    resumed.current = false;
    writeStored(SESSION_KEY, local, null);
    writeStored(TRANSCRIPT_KEY, session, null);
  }

  useEffect(() => {
    scrollRef.current?.scrollTo({
      top: scrollRef.current.scrollHeight,
      behavior: "smooth",
    });
  }, [messages, busy]);

  async function send(text: string) {
    const trimmed = text.trim();
    if (!trimmed || busy) return;
    const userMsg: Message = { id: nextId(), role: "user", text: trimmed };
    setMessages((prev) => [...prev, userMsg]);
    setInput("");
    setBusy(true);
    try {
      const res = await api.chat(trimmed, sessionId);
      if (res.session_id) {
        setSessionId(res.session_id);
        writeStored(SESSION_KEY, local, res.session_id);
      }
      setMessages((prev) => [
        ...prev,
        {
          id: nextId(),
          role: "assistant",
          text: res.answer,
          citations: res.citations,
          caveats: res.caveats,
          followups: res.followups,
        },
      ]);
    } catch (err) {
      setMessages((prev) => [
        ...prev,
        {
          id: nextId(),
          role: "assistant",
          text:
            err instanceof ApiError && err.status === 0
              ? "I cannot reach the local service right now. Once heliosd is running I can answer from your own data."
              : "Something went wrong answering that.",
        },
      ]);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex h-full flex-col animate-fade">
      <header className="flex items-start justify-between gap-3 pb-3">
        <div>
          <h1 className="font-serif text-3xl">Ask Helios</h1>
          <p className="mt-1 text-sm text-muted">
            Answers come from your own data, with sources shown.
            {resumed.current && sessionId
              ? " Continuing your earlier conversation; the Mac remembers its turns."
              : ""}
          </p>
        </div>
        {sessionId ? (
          <button
            onClick={startNew}
            className="shrink-0 rounded-full border border-hairline px-3 py-1 text-xs text-muted transition-colors hover:bg-hairline/40"
          >
            New conversation
          </button>
        ) : null}
      </header>

      <div
        ref={scrollRef}
        className="no-scrollbar flex-1 space-y-4 overflow-y-auto py-2"
      >
        {messages.length === 0 ? (
          <div className="rounded-2xl border border-hairline bg-surface p-5 text-sm text-muted">
            Try asking things like "How has my sleep been this week?" or "Is my
            HRV trending down?"
          </div>
        ) : null}

        {messages.map((m) =>
          m.role === "user" ? (
            <div key={m.id} className="flex justify-end">
              <div className="max-w-[85%] rounded-2xl rounded-br-md bg-mint/15 px-4 py-2.5 text-sm text-text">
                {m.text}
              </div>
            </div>
          ) : (
            <div key={m.id} className="flex justify-start">
              <div className="max-w-[90%] space-y-3">
                <div className="rounded-2xl rounded-bl-md border border-hairline bg-surface px-4 py-3 text-sm leading-relaxed text-text/90">
                  {m.text}
                </div>
                {m.citations && m.citations.length > 0 ? (
                  <div className="flex flex-wrap gap-2">
                    {m.citations.map((c, i) => (
                      <CitationChip key={i} c={c} />
                    ))}
                  </div>
                ) : null}
                {m.caveats && m.caveats.length > 0 ? (
                  <ul className="space-y-1 pl-1">
                    {m.caveats.map((cav, i) => (
                      <li key={i} className="text-[12px] text-muted">
                        Note: {cav}
                      </li>
                    ))}
                  </ul>
                ) : null}
                {m.followups && m.followups.length > 0 ? (
                  <div className="flex flex-wrap gap-2">
                    {m.followups.map((f, i) => (
                      <button
                        key={i}
                        onClick={() => send(f)}
                        className="rounded-full border border-hairline px-3 py-1 text-xs text-mint transition-colors hover:bg-mint/10"
                      >
                        {f}
                      </button>
                    ))}
                  </div>
                ) : null}
              </div>
            </div>
          )
        )}

        {busy ? (
          <div className="flex justify-start">
            <div className="rounded-2xl rounded-bl-md border border-hairline bg-surface px-4 py-3 text-sm text-muted">
              Thinking...
            </div>
          </div>
        ) : null}
      </div>

      <div className="space-y-3 pt-3">
        <QuickLog />
        <div className="flex items-center gap-2">
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") send(input);
            }}
            placeholder="Ask about your health data"
            className="min-w-0 flex-1 rounded-full border border-hairline bg-surface px-4 py-2.5 text-sm text-text outline-none placeholder:text-muted focus:border-mint/50"
          />
          <button
            onClick={() => send(input)}
            disabled={busy || !input.trim()}
            className="shrink-0 rounded-full px-4 py-2.5 text-sm font-medium transition-colors disabled:opacity-40"
            style={{ color: "var(--bg)", backgroundColor: "var(--mint)" }}
          >
            Send
          </button>
        </div>
      </div>
    </div>
  );
}
