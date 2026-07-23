import { useState } from "react";
import { TabBar } from "./components/TabBar";
import type { TabKey } from "./components/TabBar";
import { Today } from "./screens/Today";
import { Sleep } from "./screens/Sleep";
import { Activity } from "./screens/Activity";
import { Labs } from "./screens/Labs";
import { Insights } from "./screens/Insights";
import { Actions } from "./screens/Actions";
import { Chat } from "./screens/Chat";

function ChatFab({ onClick }: { onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      aria-label="Ask Helios"
      className="fixed bottom-24 right-5 z-40 flex h-14 w-14 items-center justify-center rounded-full shadow-lg transition-transform active:scale-95"
      style={{ backgroundColor: "var(--mint)", color: "var(--bg)" }}
    >
      <svg
        width="24"
        height="24"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.9"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <path d="M21 12a8 8 0 0 1-8 8H5l-2 2V12a8 8 0 0 1 16 0z" />
      </svg>
    </button>
  );
}

export default function App() {
  const [tab, setTab] = useState<TabKey>("today");

  const isChat = tab === "chat";

  return (
    <div className="min-h-screen bg-bg text-text">
      <main
        className={`mx-auto w-full max-w-app px-4 safe-top ${
          isChat ? "flex h-[100dvh] flex-col pb-24 pt-4" : "pb-28 pt-6"
        }`}
      >
        {tab === "today" && <Today />}
        {tab === "sleep" && <Sleep />}
        {tab === "activity" && <Activity />}
        {tab === "labs" && <Labs />}
        {tab === "insights" && <Insights />}
        {tab === "actions" && <Actions />}
        {tab === "chat" && <Chat />}
      </main>

      {!isChat ? <ChatFab onClick={() => setTab("chat")} /> : null}

      <TabBar
        active={isChat ? "today" : tab}
        onChange={(t) => setTab(t)}
      />
    </div>
  );
}
