import { TABS } from "../lib/nav";
import type { TabKey } from "../lib/nav";

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
    case "history":
      // A clock with a turn-back arrow: the record so far.
      return (
        <svg {...common}>
          <path d="M3 12a9 9 0 1 0 2.6-6.4" />
          <path d="M3 4v4h4" />
          <path d="M12 7v5l3 2" />
        </svg>
      );
    case "system":
      // A gauge: is the pipeline healthy.
      return (
        <svg {...common}>
          <path d="M4.5 17a8.5 8.5 0 1 1 15 0" />
          <path d="M12 13l4-4" />
          <circle cx="12" cy="13" r="1" />
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
