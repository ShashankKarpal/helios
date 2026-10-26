import SwiftUI
import HealthKit
import BridgeCore
import GlanceCore

// MARK: - Status screen
struct StatusView: View {
    @State private var manager = HealthKitManager.shared
    @State private var settings = Settings.shared
    @State private var glance = GlanceManager.shared
    @State private var busyRows: Set<String> = []
    @State private var rereadFor: StreamDef?
    @State private var showOtherTypes = false
    @State private var showWakes = false

    private static let relative: RelativeDateTimeFormatter = {
        let f = RelativeDateTimeFormatter()
        f.unitsStyle = .abbreviated
        return f
    }()

    var body: some View {
        NavigationStack {
            Form {
                authorizationSection
                if let note = manager.setupNote {
                    Section { Text(note).font(.footnote).foregroundStyle(.orange) }
                }
                glanceSection
                summarySection
                syncButtonSection
                if let snap = manager.snapshot, snap.outbox.parked > 0 { parkedSection(snap) }
                if let jobs = manager.snapshot?.jobs, !jobs.isEmpty { jobsSection(jobs) }
                typesSection
                wakesSection
                settingsSection
                signingNoteSection
            }
            .navigationTitle("Helios Bridge")
            .sheet(item: $rereadFor) { def in
                RereadSheet(def: def) { from, to in
                    Task { await manager.reread(def, from: from, to: to) }
                }
            }
        }
    }

    // MARK: Authorization
    @ViewBuilder
    private var authorizationSection: some View {
        if !manager.authorizationRequested {
            Section {
                Button {
                    Task { await manager.requestAuthorizationAndStart() }
                } label: {
                    Label("Grant Health access and start", systemImage: "heart.text.square")
                }
            } footer: {
                Text("Helios Bridge reads your Health data and sends it to your Mac. It never writes to Health.")
            }
        } else if manager.needsNewPermissions {
            Section {
                Button {
                    Task { await manager.requestAuthorizationAndStart() }
                } label: {
                    Label("Allow the new Health types", systemImage: "plus.circle.fill")
                        .font(.headline)
                }
            } footer: {
                Text("Workouts with their routes, ECG recordings, activity rings and \(TypeRegistry.extraQuantities.count + TypeRegistry.extraCategories.count) more types. iOS asks only for the types not decided yet; tap Turn On All. Their history then starts to send; keep the app open while it does.")
            }
        }
    }

    // MARK: Summary
    private var summarySection: some View {
        let snap = manager.snapshot
        return Section("Status") {
            LabeledContent("Last delivery") { Text(relative(manager.lastDeliveryDate)) }
            LabeledContent("Mac link") {
                if let failure = snap?.linkFailure {
                    Text(failure).foregroundStyle(.orange)
                } else {
                    Text(snap?.linkVerified == true ? "ok" : "untested").foregroundStyle(.secondary)
                }
            }
            LabeledContent("Outbox") {
                let o = snap?.outbox
                Text(o.map { "\($0.queued) queued" + ($0.parked > 0 ? ", \($0.parked) parked" : "")
                    + ($0.quarantined > 0 ? ", \($0.quarantined) kept aside" : "") } ?? "-")
                    .foregroundStyle((o?.queued ?? 0) + (o?.parked ?? 0) > 0 ? .orange : .secondary)
            }
            if let held = snap?.retries.values.reduce(0, +), held > 0 {
                LabeledContent("Read again later") { Text("\(held) records").foregroundStyle(.orange) }
                let refused = snap?.blocks.keys.contains { $0.hasPrefix("retry:") } == true
                Text("Records whose parts (a route's points, an ECG's voltages) could not be read yet. They stay on this list until they are sent."
                     + (refused ? " The Mac refused some of them; they are tried again after an hour, or now with Sync Now." : ""))
                    .font(.caption2).foregroundStyle(.secondary)
            }
            if let snap, snap.outboxUnreadable > 0 || snap.cursorsUnreadable {
                Text("Some sync files cannot be read yet (the phone has not been unlocked since it restarted). Nothing is overwritten; they are read again at the next sync.")
                    .font(.caption2).foregroundStyle(.orange)
            }
            if let error = snap?.bookkeepingError {
                Text("Retry bookkeeping not saved: \(error)").font(.caption2).foregroundStyle(.orange)
            }
        }
    }

    // MARK: Sync now
    private var syncButtonSection: some View {
        Section {
            Button {
                Task { await manager.runForeground(reason: "manual", force: true) }
            } label: {
                HStack {
                    Spacer()
                    if manager.syncing {
                        ProgressView()
                    } else {
                        Label("Sync Now", systemImage: "arrow.triangle.2.circlepath").font(.headline)
                    }
                    Spacer()
                }
                .padding(.vertical, 6)
            }
            .disabled(manager.syncing || !manager.authorizationRequested)
        }
    }

    private func parkedSection(_ snap: EngineSnapshot) -> some View {
        Section {
            Button("Retry refused pages") { Task { await manager.retryParked() } }
        } footer: {
            Text("\(snap.outbox.parked) pages were refused by the Mac and are kept on the phone. Retry once the Mac is fixed; nothing is deleted.")
        }
    }

    private func jobsSection(_ jobs: [RereadJob]) -> some View {
        Section("Re-reads") {
            ForEach(jobs) { job in
                HStack {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(TypeRegistry.byKey[job.stream]?.shortName ?? job.stream).font(.subheadline)
                        Text("\(job.start.formatted(date: .abbreviated, time: .omitted)) to \(job.end.formatted(date: .abbreviated, time: .omitted)), \(Int(job.progress * 100)) percent"
                             + (job.lastError.map { ", waiting (\($0))" } ?? ""))
                            .font(.caption2).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button(role: .destructive) {
                        Task { await manager.cancelReread(job.id) }
                    } label: { Image(systemName: "xmark.circle") }
                    .buttonStyle(.plain)
                }
            }
        }
    }

    // MARK: Types
    private var typesSection: some View {
        let allowed = Set(manager.allowedStreams.map(\.key))
        let main = (TypeRegistry.registered + TypeRegistry.classes).filter { allowed.contains($0.key) }
        let other = (TypeRegistry.extraQuantities + TypeRegistry.extraCategories).filter { allowed.contains($0.key) }
        return Section {
            ForEach(main, id: \.key) { def in row(def) }
            if !other.isEmpty {
                DisclosureGroup(isExpanded: $showOtherTypes) {
                    ForEach(other, id: \.key) { def in row(def) }
                } label: {
                    let sent = other.reduce(0) { $0 + (manager.status($1)?.sentRecords ?? 0) }
                    let running = other.contains { manager.status($0)?.running == true }
                    Text("Other types (\(other.count))" + (sent > 0 ? ", \(sent) records sent" : "") + (running ? ", sending" : ""))
                }
            }
        } header: {
            Text("Types")
        } footer: {
            Text("Time on the right is the newest record sent. Tap the arrow to sync one type now. Swipe left for Reset and re-pull (the whole history again; the Mac de-duplicates) or Re-read dates (one type over chosen days).")
        }
    }

    private func row(_ def: StreamDef) -> some View {
        let status = manager.status(def)
        return HStack(spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                HStack {
                    Text(def.shortName).font(.subheadline.weight(.medium))
                    Spacer()
                    Text(relative(status?.newestRecord)).font(.caption).foregroundStyle(.secondary)
                }
                Text(statusLine(status, held: manager.snapshot?.retries[def.key] ?? 0)).font(.caption2)
                    .foregroundStyle(status?.lastError != nil && status?.waitingForUnlock != true ? .red : .secondary)
            }
            if busyRows.contains(def.key) || status?.running == true {
                ProgressView().frame(width: 44, height: 44)
            } else {
                Button {
                    runRow(def) { await manager.syncNow(def) }
                } label: {
                    Image(systemName: "arrow.triangle.2.circlepath").font(.body)
                        .frame(width: 44, height: 44).contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .disabled(!manager.authorizationRequested)
            }
        }
        .swipeActions(edge: .trailing, allowsFullSwipe: false) {
            Button {
                runRow(def) { await manager.reset(def) }
            } label: {
                Label("Reset & re-pull", systemImage: "arrow.clockwise.circle")
            }
            .tint(.orange)
            if def.cls == .quantity || def.cls == .category {
                Button {
                    rereadFor = def
                } label: {
                    Label("Re-read dates", systemImage: "calendar")
                }
                .tint(.blue)
            }
        }
    }

    /// Per-type progress (C5): what was sent and how far the history drain is.
    private func statusLine(_ s: StreamStatus?, held: Int) -> String {
        guard let s else { return "waiting for the first sync" }
        if s.waitingForUnlock { return "waiting for unlock" }
        if let e = s.lastError { return e }
        var parts: [String] = []
        if s.sentRecords > 0 { parts.append("\(s.sentRecords) sent") }
        if held > 0 { parts.append("\(held) to read again") }
        if s.running, let p = s.position { parts.append("history at \(p.formatted(.dateTime.month(.abbreviated).year()))") }
        if s.caughtUp { parts.append("up to date") }
        if let q = s.relayQueued { parts.append("relay holds \(q)") }
        return parts.isEmpty ? "no new records" : parts.joined(separator: ", ")
    }

    private func runRow(_ def: StreamDef, _ work: @escaping () async -> Void) {
        guard !busyRows.contains(def.key) else { return }
        busyRows.insert(def.key)
        Task { @MainActor in
            await work()
            busyRows.remove(def.key)
        }
    }

    // MARK: Wakes (what iOS allows)
    private var wakesSection: some View {
        Section {
            DisclosureGroup(isExpanded: $showWakes) {
                ForEach(Array(manager.recentWakes.enumerated()), id: \.offset) { _, w in
                    VStack(alignment: .leading, spacing: 1) {
                        Text("\(w.at.formatted(date: .omitted, time: .shortened)) \(w.kind)" + (w.detail.map { " \($0)" } ?? ""))
                            .font(.caption)
                        Text("\(w.outcome ?? "") in \(w.seconds.map { String(format: "%.1f", $0) } ?? "?") s")
                            .font(.caption2).foregroundStyle(.secondary)
                    }
                }
            } label: {
                let counts = manager.wakeCounts.sorted { $0.key < $1.key }.map { "\($0.key) \($0.value)" }
                Text(counts.isEmpty ? "Wakes: none logged yet" : "Wakes in 24 h: " + counts.joined(separator: ", "))
                    .font(.subheadline)
            }
        } footer: {
            Text("When iOS let the Bridge run, and what it achieved. Health data is unreadable while the phone is locked; those wakes say locked.")
        }
    }

    // MARK: Morning summary (rank 3)
    // The same state and numbers as the widget; numbers here only show inside
    // the app, which needs an unlocked phone.
    private var glanceSection: some View {
        let state = glance.state
        let display = glanceDisplay(state, cache: glance.cache, when: { glanceWhen($0, now: Date()) })
        return Section {
            Text(state == .notSetUp ? "Type the Mac's Tailscale name and port in Summary from, below." : display.headline)
                .font(.headline)
            ForEach(display.rows, id: \.metric) { row in
                LabeledContent(row.label) {
                    HStack(spacing: 4) {
                        if row.state == "flag" {
                            Image(systemName: "exclamationmark.circle").accessibilityLabel("off baseline")
                        }
                        Text(row.text).monospacedDigit()
                    }
                }
                .foregroundStyle(display.numbersCurrent ? .primary : .secondary)
            }
            if display.numbersCurrent, let verdict = glance.cache.reply?.summary?.verdict, !verdict.isEmpty {
                Text(verdict).font(.footnote)
            }
            if let line = display.dataLine {
                Text(line).font(.caption).foregroundStyle(.secondary)
            }
            if let note = display.footnote {
                Text(note).font(.caption).foregroundStyle(.orange)
            }
            Button("Check now") {
                Task { await glance.refresh(reason: "manual", force: true) }
            }
            .disabled(state == .notSetUp)
        } header: {
            Text("Morning summary")
        } footer: {
            Text(lastCheck)
        }
    }

    private var lastCheck: String {
        guard let at = glance.cache.lastAttempt else { return "Not checked yet." }
        let when = glanceWhen(at, now: Date())
        switch glance.cache.lastFailure {
        case .none: return "Last check \(when): ok."
        case .link(let reason)?: return "Last check \(when): \(reason)."
        case .http(401)?: return "Last check \(when): the Mac refused the token (HTTP 401)."
        case .http(let code)?: return "Last check \(when): HTTP \(code)."
        case .unreadable?: return "Last check \(when): the reply was not a summary."
        }
    }

    // MARK: Settings
    private var settingsSection: some View {
        @Bindable var settings = settings
        return Section {
            LabeledContent("Host") {
                TextField(Settings.defaultHost, text: $settings.host)
                    .multilineTextAlignment(.trailing)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .keyboardType(.URL)
            }
            LabeledContent("Token") {
                SecureField("X-Helios-Token", text: $settings.token)
                    .multilineTextAlignment(.trailing)
            }
            LabeledContent("Summary from") {
                TextField("Mac name:8420", text: $settings.summaryHost)
                    .multilineTextAlignment(.trailing)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .keyboardType(.URL)
            }
        } header: {
            Text("Mac")
        } footer: {
            Text("Summary from is the Mac itself (its Tailscale name and port), for the widget and the morning notification. The Host can stay the relay.")
        }
    }

    private var signingNoteSection: some View {
        Section {
            Label {
                Text("This build is signed with an Apple Developer Program certificate and provisioning profile that expire about one year after signing. Re-sign and reinstall from Xcode before then to keep background sync alive.")
                    .font(.caption)
            } icon: {
                Image(systemName: "signature")
            }
        }
    }

    private func relative(_ date: Date?) -> String {
        guard let date else { return "never" }
        return Self.relative.localizedString(for: date, relativeTo: Date())
    }
}

// MARK: - Re-read sheet (C8)
struct RereadSheet: View {
    let def: StreamDef
    let start: (Date, Date) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var from = Calendar.current.date(byAdding: .day, value: -7, to: Date()) ?? Date()
    @State private var to = Date()

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    DatePicker("From", selection: $from, in: ...to, displayedComponents: .date)
                    DatePicker("To", selection: $to, in: from..., displayedComponents: .date)
                } footer: {
                    Text("Reads \(def.shortName) again for these days by date, the read path that finds records the normal one can miss, and sends them. The Mac keeps one copy of each.")
                }
            }
            .navigationTitle("Re-read \(def.shortName)")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Re-read") {
                        let cal = Calendar.current
                        let endOfTo = cal.date(byAdding: .day, value: 1, to: cal.startOfDay(for: to)) ?? to
                        start(cal.startOfDay(for: from), min(endOfTo, Date()))
                        dismiss()
                    }
                }
            }
        }
    }
}

#Preview {
    StatusView()
}
