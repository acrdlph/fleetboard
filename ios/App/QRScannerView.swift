import SwiftUI
import AVFoundation

/// The camera path.
///
/// **A simulator has no camera**, and `AVCaptureDevice.default(for: .video)`
/// returns nil there rather than throwing — so this sheet is written to say so
/// out loud instead of showing a black rectangle that looks like a broken
/// preview. That black rectangle is exactly the class of silent failure this
/// project keeps finding, and it would be the first thing anybody testing on a
/// simulator would see.
struct QRScannerSheet: View {
    let onScan: (String) -> Void
    @Environment(\.dismiss) private var dismiss

    @State private var cameraAvailable = QRScannerView.hasCamera

    var body: some View {
        NavigationStack {
            ZStack {
                Palette.canvas.ignoresSafeArea()
                if cameraAvailable {
                    QRScannerView(onScan: onScan)
                        .ignoresSafeArea()
                } else {
                    VStack(spacing: Space.md) {
                        Image(systemName: "camera.metering.unknown")
                            .font(.system(size: 40))
                            .foregroundStyle(Palette.textTertiary)
                        Text("no camera on this device")
                            .font(OrcFont.title)
                            .foregroundStyle(Palette.textPrimary)
                        Text("Type the code instead — the board shows it beside the "
                             + "QR, grouped for reading aloud.")
                            .font(OrcFont.bodyCompact)
                            .foregroundStyle(Palette.textSecondary)
                            .multilineTextAlignment(.center)
                    }
                    .padding(Space.xl)
                }
            }
            .navigationTitle("scan")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button("Cancel") { dismiss() }
                }
            }
        }
    }
}

/// A thin `UIViewControllerRepresentable` over `AVCaptureMetadataOutput`.
/// The one `@unchecked Sendable` in this app, and it is narrow on purpose.
///
/// `AVCaptureSession` is not `Sendable`, but `startRunning()` blocks for as long
/// as the camera takes to configure — which on the main actor is a visibly
/// stalled sheet. AVFoundation documents `startRunning()`/`stopRunning()` as
/// safe to call off the main queue, and those two methods are the ONLY members
/// this box exposes; every other use of the session in this file happens on the
/// main actor. That is the whole justification, and it does not extend to
/// anything else.
private struct SessionBox: @unchecked Sendable {
    private let session: AVCaptureSession
    init(_ session: AVCaptureSession) { self.session = session }
    func start() { session.startRunning() }
}

struct QRScannerView: UIViewControllerRepresentable {
    let onScan: (String) -> Void

    static var hasCamera: Bool {
        AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: .back) != nil
    }

    func makeUIViewController(context: Context) -> ScannerController {
        let controller = ScannerController()
        controller.onScan = onScan
        return controller
    }

    func updateUIViewController(_ controller: ScannerController, context: Context) {
        controller.onScan = onScan
    }

    /// The callback arrives on whatever queue the delegate was registered with,
    /// and it is registered with `.main` — so `MainActor.assumeIsolated` inside
    /// `metadataOutput` is a statement of a fact this file establishes four
    /// lines away, not a silencer. (`@preconcurrency` is not needed here: the
    /// SDK already declares this protocol main-actor-isolated in Xcode 26, and
    /// adding it is a warning, which this target treats as an error.)
    @MainActor
    final class ScannerController: UIViewController,
                                   AVCaptureMetadataOutputObjectsDelegate {
        var onScan: ((String) -> Void)?
        private let session = AVCaptureSession()
        private var preview: AVCaptureVideoPreviewLayer?
        private var delivered = false

        override func viewDidLoad() {
            super.viewDidLoad()
            view.backgroundColor = .black
            guard let device = AVCaptureDevice.default(.builtInWideAngleCamera,
                                                       for: .video, position: .back),
                  let input = try? AVCaptureDeviceInput(device: device),
                  session.canAddInput(input) else { return }
            session.addInput(input)

            let output = AVCaptureMetadataOutput()
            guard session.canAddOutput(output) else { return }
            session.addOutput(output)
            output.setMetadataObjectsDelegate(self, queue: .main)
            output.metadataObjectTypes = [.qr]

            let layer = AVCaptureVideoPreviewLayer(session: session)
            layer.videoGravity = .resizeAspectFill
            layer.frame = view.bounds
            view.layer.addSublayer(layer)
            preview = layer
        }

        override func viewDidLayoutSubviews() {
            super.viewDidLayoutSubviews()
            preview?.frame = view.bounds
        }

        override func viewWillAppear(_ animated: Bool) {
            super.viewWillAppear(animated)
            guard !session.isRunning else { return }
            // `startRunning()` BLOCKS — Apple's own guidance is to call it off
            // the main queue, and on the main actor it is a dropped frame at
            // best. See `SessionBox` for why the hop is safe.
            let box = SessionBox(session)
            Task.detached(priority: .userInitiated) { box.start() }
        }

        override func viewWillDisappear(_ animated: Bool) {
            super.viewWillDisappear(animated)
            session.stopRunning()
        }

        nonisolated func metadataOutput(_ output: AVCaptureMetadataOutput,
                                        didOutput objects: [AVMetadataObject],
                                        from connection: AVCaptureConnection) {
            // The ONLY thing that crosses the isolation boundary is a `String`,
            // which is `Sendable`. `AVMetadataObject` is not, so it is unwrapped
            // here — on the delegate's own queue — and never captured.
            guard let scanned = objects
                .compactMap({ $0 as? AVMetadataMachineReadableCodeObject })
                .compactMap(\.stringValue)
                .first
            else { return }
            Task { @MainActor [weak self] in self?.deliver(scanned) }
        }

        /// ONE delivery, and ONLY of a pairing payload. A QR in frame produces a
        /// callback per FRAME, and a second claim of the same code is refused as
        /// `pairing_not_open` — which would read to the user as "it did not work"
        /// a tenth of a second after it did.
        ///
        /// A scanner pointed at a Wi-Fi QR, a URL, a contact card — anything that
        /// is not `orc://p?…` with the fields the pairing flow needs — must not
        /// latch and dismiss the sheet on the first code in frame. So the payload
        /// is validated with the pairing parser itself (`PairingTicket(url:)`, the
        /// same check `PairingScreen` and `onOpenURL` use — one definition of "a
        /// pairing QR"): a non-match is ignored and scanning continues; only a
        /// valid ticket latches, stops the session, and delivers.
        private func deliver(_ scanned: String) {
            guard !delivered else { return }
            guard PairingTicket(url: scanned) != nil else { return }
            delivered = true
            session.stopRunning()
            onScan?(scanned)
        }
    }
}
