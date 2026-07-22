import SwiftUI
import HealthKit

// MARK: - Status screen
struct StatusView: View {
    @State private var manager = HealthKitManager.shared
    @State private var outbox = Outbox.shared
    @State private var settings = Settings.shared

    @State private var isSyncing = false

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
            LabeledContent("Last sync") {
                Text(relative(manager.lastSyncDate))
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
        Section("Types") {
            ForEach(HealthTypes.all, id: \.identifier) { def in
                let status = manager.perTypeStatus[def.identifier]
                VStack(alignment: .leading, spacing: 2) {
                    HStack {
                        Text(HealthTypes.shortName(def.identifier))
                            .font(.subheadline.weight(.medium))
                        Spacer()
                        Text(relative(status?.lastSampleDate))
                            .font(.caption)
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
            }
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
