import SwiftUI
import WidgetKit
import GlanceCore

// MARK: - Helios widgets (rank 3)
// Two widgets, both called "Helios", one per place:
// - Home Screen (small): the status line, the four morning numbers and when
//   they are from. Its background cannot be removed, which keeps it out of
//   StandBy (Apple: a nonremovable background "excludes your widget from the
//   widget gallery in contexts that require a removable background ... in
//   StandBy"), and CarPlay and iPhone widgets on a Mac are disfavored. The
//   numbers are privacy sensitive, and when iOS asks for privacy the widget
//   draws the status line alone.
// - Lock Screen (rectangular): the status line only, locked or not. No health
//   number ever appears there (owner decision 2026-10-26).
//
// Both read the cache the app shares in the App Group and, when it is due,
// ask heliosd themselves (the only way the Lock Screen can show the morning
// brief before the first unlock). System fonts and colours only: no brand
// palette value or brand asset lives in this extension.

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
            let next = glanceNextRefresh(cache, configured: Self.configured, now: now)
            completion(Timeline(entries: entries, policy: .after(next)))
        }
    }

    static var shared: UserDefaults? { UserDefaults(suiteName: GlanceShared.appGroup) }

    static var configured: Bool {
        GlanceClient.isValidAddress(shared?.string(forKey: GlanceShared.summaryHostKey) ?? "")
    }

    /// The shared cache, refreshed from heliosd first when it is due. A
    /// cancelled fetch records nothing; a failed save keeps the merged reply.
    static func refreshedCache(now: Date) async -> GlanceCache {
        guard let store = GlanceShared.store() else { return GlanceCache() }
        let cache = store.load()
        let host = shared?.string(forKey: GlanceShared.summaryHostKey) ?? ""
        let token = shared?.string(forKey: GlanceShared.tokenKey) ?? ""
        guard GlanceClient.isValidAddress(host), cache.due(now: now) else { return cache }
        guard let result = await GlanceClient.fetch(host: host, token: token) else { return cache }
        return store.merge(cache, attemptStart: now, result: result)
    }

    static func entry(at date: Date, cache: GlanceCache) -> GlanceEntry {
        let state = glanceState(cache, configured: configured, now: date)
        return GlanceEntry(date: date, display: glanceDisplay(state, cache: cache, when: { glanceWhen($0, now: date) }))
    }
}

/// Home Screen: the status line and the four numbers, or the status line alone
/// when iOS asks for privacy.
struct GlanceHomeView: View {
    @Environment(\.redactionReasons) private var redaction
    let entry: GlanceEntry

    var body: some View {
        Group {
            if surfaceShowsNumbers(.homeScreen, privacyRedacted: redaction.contains(.privacy)) {
                numbers
            } else {
                GlanceStatusOnly(headline: entry.display.headline)
            }
        }
        .widgetURL(URL(string: "helios-bridge://glance"))
        .containerBackground(.fill.tertiary, for: .widget)
    }

    private var numbers: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(entry.display.headline)
                .font(.caption.bold())
                .lineLimit(2)
                .minimumScaleFactor(0.8)
            ForEach(entry.display.rows, id: \.metric) { row in
                HStack(spacing: 3) {
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
                    if let note = row.note {
                        // Another device's value or a running total: never shown as final.
                        Text(note).font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                    }
                }
                .minimumScaleFactor(0.6)
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

struct GlanceStatusOnly: View {
    let headline: String

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Helios").font(.caption.bold()).foregroundStyle(.secondary)
            Text(headline).font(.subheadline.bold()).lineLimit(3)
            Spacer(minLength: 0)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// Lock Screen: the status line only, never a number. It is given the line
/// (glanceLockLine) and nothing else.
struct GlanceLockView: View {
    let line: String

    var body: some View {
        VStack(alignment: .leading, spacing: 1) {
            Text("Helios").font(.headline).widgetAccentable()
            Text(line)
                .font(.caption)
                .lineLimit(2)
                .minimumScaleFactor(0.8)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
        .widgetURL(URL(string: "helios-bridge://glance"))
        .containerBackground(.fill.tertiary, for: .widget)
    }
}

struct HeliosGlanceWidget: Widget {
    let kind = "HeliosGlance"

    var body: some WidgetConfiguration {
        StaticConfiguration(kind: kind, provider: GlanceProvider()) { entry in
            GlanceHomeView(entry: entry)
        }
        .configurationDisplayName("Helios")
        .description("This morning's brief and its four numbers.")
        .supportedFamilies([.systemSmall])
        .containerBackgroundRemovable(false)
        .disfavoredLocations(Self.disfavored, for: [.systemSmall])
    }

    static var disfavored: [WidgetLocation] {
        var places: [WidgetLocation] = [.iPhoneWidgetsOnMac]
        if #available(iOS 26.0, *) { places.append(.carPlay) }
        return places
    }
}

struct HeliosStatusWidget: Widget {
    let kind = "HeliosStatus"

    var body: some WidgetConfiguration {
        StaticConfiguration(kind: kind, provider: GlanceProvider()) { entry in
            GlanceLockView(line: glanceLockLine(entry.display))
        }
        .configurationDisplayName("Helios")
        .description("This morning's brief: ready, waiting, late, or Mac not reachable. No numbers.")
        .supportedFamilies([.accessoryRectangular])
    }
}

@main
struct HeliosWidgets: WidgetBundle {
    var body: some Widget {
        HeliosGlanceWidget()
        HeliosStatusWidget()
    }
}
