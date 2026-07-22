export type TabKey = "today" | "sleep" | "activity" | "insights" | "actions" | "chat";

interface Tab {
  key: TabKey;
  label: string;
  icon: string;
}

const TABS: Tab[] = [
  { key: "today", label: "Today", icon: "sun" },
  { key: "sleep", label: "Sleep", icon: "moon" },
  { key: "activity", label: "Activity", icon: "pulse" },
  { key: "insights", label: "Insights", icon: "spark" },
  { key: "actions", label: "Actions", icon: "check" },
];

function Icon({ name, active }: { name: string; active: boolean }) {
  const stroke = active ? "var(--mint)" : "var(--muted)";
  const common = {
    width: 22,
    height: 22,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke,
    strokeWidth: 1.7,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
  };
  switch (name) {
    case "sun":
      return (
        <svg {...common}>
          <circle cx="12" cy="12" r="4" />
          <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
        </svg>
      );
    case "moon":
      return (
        <svg {...common}>
          <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" />
        </svg>
      );
    case "pulse":
      return (
        <svg {...common}>
          <path d="M3 12h4l2 6 4-14 2 8h6" />
        </svg>
      );
    case "spark":
      return (
        <svg {...common}>
          <path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z" />
        </svg>
      );
    case "check":
      return (
        <svg {...common}>
          <path d="M4 12l5 5L20 6" />
        </svg>
      );
    default:
      return null;
  }
}

interface Props {
  active: TabKey;
  onChange: (tab: TabKey) => void;
}

export function TabBar({ active, onChange }: Props) {
  return (
    <nav className="fixed inset-x-0 bottom-0 z-30 border-t border-hairline bg-bg/85 backdrop-blur-md safe-bottom">
      <div className="mx-auto flex max-w-app items-stretch justify-around px-2 py-2">
        {TABS.map((tab) => {
          const isActive = active === tab.key;
          return (
            <button
              key={tab.key}
              onClick={() => onChange(tab.key)}
              className="flex flex-1 flex-col items-center gap-1 rounded-xl py-1.5 transition-colors"
              aria-label={tab.label}
              aria-current={isActive ? "page" : undefined}
            >
              <Icon name={tab.icon} active={isActive} />
              <span
                className="text-[10px] tracking-wide"
                style={{ color: isActive ? "var(--mint)" : "var(--muted)" }}
              >
                {tab.label}
              </span>
            </button>
          );
        })}
      </div>
    </nav>
  );
}
