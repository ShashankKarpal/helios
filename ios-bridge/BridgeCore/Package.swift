// swift-tools-version:5.9
// BridgeCore: the Helios Bridge logic that does not need a phone. The durable
// outbox, the anchor and cursor stores, the sync engine, the wire models and the
// type registry live here, so `swift test` on the Mac can crash and replay them.
// The app target (project.yml) links the library; HealthKit queries stay there.
// GlanceCore (rank 3) is the widget's and the morning notification's logic,
// Foundation only, so the widget extension links it without the sync engine.
import PackageDescription

let package = Package(
    name: "BridgeCore",
    platforms: [.iOS(.v17), .macOS(.v14)],
    products: [
        .library(name: "BridgeCore", targets: ["BridgeCore"]),
        .library(name: "GlanceCore", targets: ["GlanceCore"]),
    ],
    targets: [
        .target(name: "BridgeCore"),
        .target(name: "GlanceCore"),
        // A child process the crash tests kill with SIGKILL at a chosen step.
        .executableTarget(name: "OutboxCrashHarness", dependencies: ["BridgeCore"]),
        .testTarget(name: "BridgeCoreTests", dependencies: ["BridgeCore", "OutboxCrashHarness"]),
        .testTarget(name: "GlanceCoreTests", dependencies: ["GlanceCore"]),
    ]
)
