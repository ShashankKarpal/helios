import { useEffect, useState } from "react";
import { SIGN_IN_NEEDED } from "./api";
import { SignIn } from "./components/SignIn";
import { TabBar } from "./components/TabBar";
import { DEFAULT_TAB } from "./lib/nav";
import type { TabKey } from "./lib/nav";
import { Today } from "./screens/Today";
import { History } from "./screens/History";
import { System } from "./screens/System";

export default function App() {
  const [tab, setTab] = useState<TabKey>(DEFAULT_TAB);
  const [signIn, setSignIn] = useState(false);

  useEffect(() => {
    const show = () => setSignIn(true);
    window.addEventListener(SIGN_IN_NEEDED, show);
    return () => window.removeEventListener(SIGN_IN_NEEDED, show);
  }, []);

  if (signIn) return <SignIn />;

  return (
    <div className="min-h-screen bg-bg text-text">
      <main className="mx-auto w-full max-w-app px-4 pt-safe-6 pb-28">
        {tab === "today" && <Today />}
        {tab === "history" && <History />}
        {tab === "system" && <System />}
      </main>

      <TabBar active={tab} onChange={(t) => setTab(t)} />
    </div>
  );
}
