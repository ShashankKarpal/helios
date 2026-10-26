import SwiftUI
import WidgetKit
import GlanceCore

// MARK: - Helios glance widget (rank 3)
// One widget, two sizes. The Home Screen (small) shows the status line, the
// four morning numbers and when they are from. The Lock Screen (rectangular)
// shows the status line only, locked or not: no health number ever appears
// there (owner decision 2026-10-26). The numbers on the small widget are marked
// privacy sensitive, and when iOS asks for privacy the widget draws the status
// line alone (a small widget can also appear in StandBy on a locked phone).
//
// It reads the cache the app shares in the App Group and, when that is older
// than 15 minutes, asks heliosd itself (the only way the Lock Screen can show
// the morning brief before the first unlock). System fonts and colours only:
// no brand palette value or brand asset lives in this extension.

struct GlanceEntry: TimelineEntry {
    let date: Date
    let display: GlanceDisplay

    static let sample = GlanceEntry(date: Date(), display: GlanceDisplay(
        headline: "Morning brief",
        rows: [GlanceRow(metric: "recovery_score", label: "Recovery", text: "--", state: "neutral"),
               GlanceRow(metric: "sleep_duration", label: "Sleep", text: "--", state: "neutral"),
               GlanceRow(metric: "hrv_rmssd", label: "HRV", text: "--", state: "neutral"),
               GlanceRow(metric: "resting_hr", label: "Resting HR", text: "--", state: "neutral")],
        numbersCurrent: false, dataLine: nil, footnote: nil))
}

struct GlanceProvider: TimelineProvider {
    func placeholder(in context: Context) -> GlanceEntry { .sample }

    func getSnapshot(in context: Context, completion: @escaping (GlanceEntry) -> Void) {
        if context.isPreview {
            completion(.sample)
            return
        }
        let cache = GlanceShared.store()?.load() ?? GlanceCache()
        completion(Self.entry(at: Date(), cache: cache))
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<GlanceEntry>) -> Void) {
        Task {
            let now = Date()
            let cache = await Self.refreshedCache(now: now)
            var entries = [Self.entry(at: now, cache: cache)]
            for point in glanceChangePoints(cache, now: now) {
                entries.append(Self.entry(at: point, cache: cache))
            }
            completion(Timeline(entries: entries, policy: .after(Self.nextRefresh(cache, now: now))))
        }
    }

    /// The shared cache, refreshed from heliosd first when it is due.
    static func refreshedCache(now: Date) async -> GlanceCache {
        guard let store = GlanceShared.store() else { return GlanceCache() }
        let cache = store.load()
        let shared = UserDefaults(suiteName: GlanceShared.appGroup)
        let host = shared?.string(forKey: GlanceShared.summaryHostKey) ?? ""
        let token = shared?.string(forKey: GlanceShared.tokenKey) ?? ""
        guard !host.isEmpty, cache.due(now: now) else { return cache }
        guard let result = await GlanceClient.fetch(host: host, token: token) else { return cache }
        return (try? store.update { $0.merging(attemptStart: now, result: result) }) ?? cache
    }

    static func entry(at date: Date, cache: GlanceCache) -> GlanceEntry {
        let configured = !(UserDefaults(suiteName: GlanceShared.appGroup)?
            .string(forKey: GlanceShared.summaryHostKey) ?? "").isEmpty
        let state = glanceState(cache, configured: configured, now: date)
        return GlanceEntry(date: date, display: glanceDisplay(state, cache: cache, when: { glanceWhen($0, now: date) }))
    }

    /// Every 15 minutes while a morning brief is still expected, hourly otherwise.
    static func nextRefresh(_ cache: GlanceCache, now: Date) -> Date {
        let configured = !(UserDefaults(suiteName: GlanceShared.appGroup)?
            .string(forKey: GlanceShared.summaryHostKey) ?? "").isEmpty
        switch glanceState(cache, configured: configured, now: now) {
        case .ready, .notSetUp:
            return now.addingTimeInterval(3600)
        default:
            return now.addingTimeInterval(GlanceTiming.minInterval)
        }
    }
}

struct GlanceEntryView: View {
    @Environment(\.widgetFamily) private var family
    @Environment(\.redactionReasons) private var redaction
    let entry: GlanceEntry

    var body: some View {
        Group {
            switch family {
            case .accessoryRectangular:
                lockScreen
            default:
                if redaction.contains(.privacy) {
                    statusOnly
                } else {
                    homeScreen
                }
            }
        }
        .widgetURL(URL(string: "helios-bridge://glance"))
        .containerBackground(.fill.tertiary, for: .widget)
    }

    /// Lock Screen: the status line only, never a number.
    private var lockScreen: some View {
        VStack(alignment: .leading, spacing: 1) {
            Text("Helios").font(.headline).widgetAccentable()
            Text(entry.display.headline)
                .font(.caption)
                .lineLimit(2)
                .minimumScaleFactor(0.8)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
    }

    private var statusOnly: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Helios").font(.caption.bold()).foregroundStyle(.secondary)
            Text(entry.display.headline).font(.subheadline.bold()).lineLimit(3)
            Spacer(minLength: 0)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var homeScreen: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(entry.display.headline)
                .font(.caption.bold())
                .lineLimit(2)
                .minimumScaleFactor(0.8)
            ForEach(entry.display.rows, id: \.metric) { row in
                HStack(spacing: 4) {
                    Text(row.label).font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                    Spacer(minLength: 2)
                    if row.state == "flag" {
                        // A symbol beside the colour: never colour alone.
                        Image(systemName: "exclamationmark.circle")
                            .font(.caption2)
                            .accessibilityLabel("off baseline")
                    }
                    Text(row.text)
                        .font(.caption.monospacedDigit())
                        .lineLimit(1)
                        .privacySensitive()
                }
                .foregroundStyle(entry.display.numbersCurrent ? .primary : .secondary)
            }
            Spacer(minLength: 0)
            if let line = entry.display.dataLine ?? entry.display.footnote {
                Text(line).font(.caption2).foregroundStyle(.secondary).lineLimit(1)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

@main
struct HeliosGlanceWidget: Widget {
    let kind = "HeliosGlance"

    var body: some WidgetConfiguration {
        StaticConfiguration(kind: kind, provider: GlanceProvider()) { entry in
            GlanceEntryView(entry: entry)
        }
        .configurationDisplayName("Helios")
        .description("This morning's brief: ready, waiting, late, or Mac not reachable.")
        .supportedFamilies([.systemSmall, .accessoryRectangular])
        // Suggested for the Home Screen; listed under Other for places a
        // locked phone or other people can see it.
        .disfavoredLocations(Self.disfavored, for: [.systemSmall])
    }

    static var disfavored: [WidgetLocation] {
        var places: [WidgetLocation] = [.standBy, .iPhoneWidgetsOnMac]
        if #available(iOS 26.0, *) { places.append(.carPlay) }
        return places
    }
}
