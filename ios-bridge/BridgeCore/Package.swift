// swift-tools-version:5.9
// BridgeCore: the Helios Bridge logic that does not need a phone. The durable
// outbox, the anchor and cursor stores, the sync engine, the wire models and the
// type registry live here, so `swift test` on the Mac can crash and replay them.
// The app target (project.yml) links the library; HealthKit queries stay there.
import PackageDescription

let package = Package(
    name: "BridgeCore",
    platforms: [.iOS(.v17), .macOS(.v14)],
    products: [
        .library(name: "BridgeCore", targets: ["BridgeCore"]),
    ],
    targets: [
        .target(name: "BridgeCore"),
        // A child process the crash tests kill with SIGKILL at a chosen step.
        .executableTarget(name: "OutboxCrashHarness", dependencies: ["BridgeCore"]),
        .testTarget(name: "BridgeCoreTests", dependencies: ["BridgeCore", "OutboxCrashHarness"]),
    ]
)
