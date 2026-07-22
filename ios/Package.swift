// swift-tools-version: 6.0
import PackageDescription

// Why this manifest exists alongside `Orchestra.xcodeproj`.
//
// The Xcode project builds the app — it is the only thing that produces a
// `.app` a simulator can run, and the phase gate is "xcodebuild succeeds and
// the app launches". This manifest builds the SAME source directory MINUS the
// SwiftUI layer, so `swift test` runs the model / transport / rules suites
// headless on macOS in about a second, with no simulator and no signing.
//
// One module, not the six of IOS-APP.md §1.2. The layering there is enforced by
// the package graph (`OrchestraCore` cannot see `OrchestraStore`), which is the
// right shape and which this phase deliberately does not buy yet: two build
// systems over one source tree cannot both be right about `import` statements,
// and a hand-written .pbxproj that references a local SwiftPM package is the
// single most fragile thing in this directory. Directories carry the layering
// for now; splitting them into real targets is additive and is noted as an open
// item in ios/README.md.
//
// `UI` is excluded because it is `import SwiftUI` + `UIKit`: the palette
// resolves through `UIColor(dynamicProvider:)`, which does not exist on macOS.
// Nothing under test needs it.
let package = Package(
    name: "OrchestraKit",
    platforms: [.iOS(.v18), .macOS(.v15)],
    products: [
        .library(name: "OrchestraKit", targets: ["OrchestraKit"]),
    ],
    dependencies: [],
    targets: [
        .target(
            name: "OrchestraKit",
            path: "Sources/Orchestra",
            exclude: ["UI"],
            swiftSettings: [
                .swiftLanguageMode(.v6),
            ]
        ),
        .testTarget(
            name: "OrchestraKitTests",
            dependencies: ["OrchestraKit"],
            path: "Tests/OrchestraKitTests",
            resources: [.copy("Fixtures")],
            swiftSettings: [
                .swiftLanguageMode(.v6),
            ]
        ),
    ]
)
