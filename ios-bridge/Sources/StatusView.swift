import SwiftUI
import HealthKit

// MARK: - Status screen
struct StatusView: View {
    @State private var manager = HealthKitManager.shared
    @State private var outbox = Outbox.shared
    @State private var settings = Settings.shared

    @State private var isSyncing = false
    // Types with a per-type sync currently in flight, so the row shows a spinner
    // instead of a dead-looking icon.
    @State private var syncingTypes: Set<String> = []

    private static let relativeFormatter: RelativeDateTimeFormatter = {
        let f = RelativeDateTimeFormatter()
        f.unitsStyle = .abbreviated
        return f
    }()

    var body: some View {
        NavigationStack {
            Form {
                authorizationSection
                summarySection
                syncButtonSection
                perTypeSection
                settingsSection
                signingNoteSection
            }
            .navigationTitle("Helios Bridge")
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
                Text("Helios Bridge reads your Health data and streams it to your Mac. It never writes to Health and never leaves your local network.")
            }
        }
    }

    // MARK: Summary
    private var summarySection: some View {
        Section("Status") {
            LabeledContent("Last attempt") {
                Text(relative(manager.lastSyncDate))
            }
            // The honest number: last time the Mac actually ACKED a batch.
            LabeledContent("Last delivery") {
                Text(relative(manager.lastDeliveryDate))
            }
            LabeledContent("Mac link") {
                if let reason = manager.linkFailureReason, !reason.isEmpty {
                    Text(reason)
                        .foregroundStyle(.orange)
                } else {
                    Text(manager.lastDeliveryDate != nil ? "ok" : "untested")
                        .foregroundStyle(.secondary)
                }
            }
            LabeledContent("Outbox depth") {
                Text("\(outbox.depth)")
                    .foregroundStyle(outbox.depth > 0 ? .orange : .secondary)
            }
        }
    }

    // MARK: Sync now
    private var syncButtonSection: some View {
        Section {
            Button {
                guard !isSyncing else { return }
                isSyncing = true
                Task {
                    await manager.foregroundSweep(reason: .manual)
                    isSyncing = false
                }
            } label: {
                HStack {
                    Spacer()
                    if isSyncing {
                        ProgressView()
                    } else {
                        Label("Sync Now", systemImage: "arrow.triangle.2.circlepath")
                            .font(.headline)
                    }
                    Spacer()
                }
                .padding(.vertical, 6)
            }
            .disabled(isSyncing || !manager.authorizationRequested)
        }
    }

    // MARK: Per-type freshness
    private var perTypeSection: some View {
        Section {
            ForEach(HealthTypes.all, id: \.identifier) { def in
                let status = manager.perTypeStatus[def.identifier]
                HStack(spacing: 10) {
                    VStack(alignment: .leading, spacing: 2) {
                        HStack {
                            Text(HealthTypes.shortName(def.identifier))
                                .font(.subheadline.weight(.medium))
                            Spacer()
                            Text(relative(status?.lastSampleDate))
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        // History drain position, shown only while incomplete.
                        // "history at Mar 2024" means the backfill is that deep,
                        // not that the data is stale.
                        if let bf = status?.backfillDate, status?.backfillComplete != true {
                            Text("history at \(bf.formatted(.dateTime.month(.abbreviated).year()))")
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                        }
                        if let error = status?.lastError {
                            Text(error)
                                .font(.caption2)
                                .foregroundStyle(.red)
                        } else if let count = status?.sentCount, count > 0 {
                            Text("\(count) samples sent")
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                        }
                    }
                    // Sync-one-type control. A 44pt frame plus contentShape makes
                    // this a reliable tap target (the old .caption borderless icon
                    // was far under the 44pt minimum and easy to miss), and the
                    // spinner gives feedback so it never looks dead.
                    if syncingTypes.contains(def.identifier) {
                        ProgressView()
                            .frame(width: 44, height: 44)
                    } else {
                        Button {
                            runTypeSync(def, reset: false)
                        } label: {
                            Image(systemName: "arrow.triangle.2.circlepath")
                                .font(.body)
                                .frame(width: 44, height: 44)
                                .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .disabled(!manager.authorizationRequested)
                    }
                }
                .swipeActions(edge: .trailing, allowsFullSwipe: false) {
                    Button {
                        runTypeSync(def, reset: true)
                    } label: {
                        Label("Reset & re-pull", systemImage: "arrow.clockwise.circle")
                    }
                    .tint(.orange)
                    .disabled(!manager.authorizationRequested)
                }
            }
        } header: {
            Text("Types")
        } footer: {
            Text("Time on the right is data freshness. Tap the arrow to sync one type now. A 'history at' line means the one-time backfill is still working through the past; it continues on its own. Swipe a row left and choose Reset & re-pull to clear that type's resume point and re-fetch its full history (safe: the Mac de-duplicates).")
        }
    }

    /// Runs a per-type sync, or a full reset + re-pull, with a visible spinner
    /// and a guard against double taps.
    private func runTypeSync(_ def: HealthTypeDef, reset: Bool) {
        let id = def.identifier
        guard !syncingTypes.contains(id) else { return }
        syncingTypes.insert(id)
        Task { @MainActor in
            if reset {
                await manager.resyncType(def)
            } else {
                await manager.syncTypeNow(def)
            }
            syncingTypes.remove(id)
        }
    }

    // MARK: Settings
    private var settingsSection: some View {
        @Bindable var settings = settings
        return Section("Mac") {
            LabeledContent("Host") {
                TextField("helios.local:8420", text: $settings.host)
                    .multilineTextAlignment(.trailing)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .keyboardType(.URL)
            }
            LabeledContent("Token") {
                SecureField("X-Helios-Token", text: $settings.token)
                    .multilineTextAlignment(.trailing)
            }
        }
    }

    // MARK: Signing note (Apple Developer Program context)
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

    // MARK: Helpers
    private func relative(_ date: Date?) -> String {
        guard let date else { return "never" }
        return Self.relativeFormatter.localizedString(for: date, relativeTo: Date())
    }
}

#Preview {
    StatusView()
}
