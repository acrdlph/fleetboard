import SwiftUI

/// Getting a credential onto the phone.
///
/// **Two paths, and the second one is not a courtesy.** The camera is the
/// intended path; the typed fallback is REQUIRED, because a simulator has no
/// camera and the simulator is where this gets verified. A flow whose only
/// entry point cannot be exercised in the environment that tests it is a flow
/// that ships untested.
///
/// Both paths end in the same call. The QR carries `orc://p?h=…&p=…&c=…` — the
/// CODE, never the token — and the manual form is that URL's three fields typed
/// out. `PairingTicket` is the single shape they both reduce to, so there is no
/// second claim path to keep in step.
struct PairingScreen: View {
    @Bindable var store: PairingStore

    @State private var host = ""
    @State private var port = String(PairingTicket.defaultPort)
    @State private var code = ""
    @State private var label = defaultLabel()
    @State private var scanning = false
    @FocusState private var focus: Field?

    private enum Field: Hashable { case host, port, code, label }

    /// Normalised as the user types, exactly as the server will normalise it —
    /// so the eight characters shown back are the eight that will be compared.
    private var ticket: PairingTicket? {
        let normalised = PairingTicket.normalise(code)
        guard normalised.count == 8,
              !host.trimmingCharacters(in: .whitespaces).isEmpty,
              let p = Int(port), (1...65535).contains(p)
        else { return nil }
        return PairingTicket(host: host.trimmingCharacters(in: .whitespaces),
                             port: p, code: normalised)
    }

    var body: some View {
        NavigationStack {
            ZStack {
                Palette.canvas.ignoresSafeArea()
                BodyWash()
                ScrollView {
                    VStack(alignment: .leading, spacing: Space.lg) {
                        intro
                        scanButton
                        divider
                        manualForm
                        pairButton
                        status
                    }
                    .padding(Space.lg)
                }
                .scrollDismissesKeyboard(.interactively)
            }
            .navigationTitle("pair this phone")
            .navigationBarTitleDisplayMode(.inline)
        }
        .sheet(isPresented: $scanning) {
            QRScannerSheet { scanned in
                scanning = false
                guard let ticket = PairingTicket(url: scanned) else { return }
                host = ticket.host
                port = String(ticket.port)
                code = PairingTicket.grouped(ticket.code)
            }
        }
    }

    private var intro: some View {
        VStack(alignment: .leading, spacing: Space.sm) {
            Text("On the Mac, open the board and choose ＋ pair a device.")
                .font(OrcFont.bodyCompact)
                .foregroundStyle(Palette.textSecondary)
            Text("The code is good for 120 seconds and for one phone. It is not the "
                 + "credential — claiming it mints a fresh one that was never on screen.")
                .font(OrcFont.meta)
                .foregroundStyle(Palette.textTertiary)
        }
    }

    private var scanButton: some View {
        Button {
            scanning = true
        } label: {
            Label("Scan the code", systemImage: "qrcode.viewfinder")
                .font(OrcFont.button)
                .frame(maxWidth: .infinity, minHeight: 44)
        }
        .foregroundStyle(Palette.statusFree)
        .overlay(
            RoundedRectangle(cornerRadius: Radius.sm, style: .continuous)
                .stroke(Palette.controlStrong, lineWidth: 1)
        )
    }

    private var divider: some View {
        HStack(spacing: Space.sm) {
            Rectangle().fill(Palette.hairline).frame(height: 1)
            Text("OR TYPE IT")
                .font(OrcFont.label)
                .orcTracking(11)
                .foregroundStyle(Palette.textTertiary)
            Rectangle().fill(Palette.hairline).frame(height: 1)
        }
    }

    private var manualForm: some View {
        VStack(alignment: .leading, spacing: Space.md) {
            field("MAC ADDRESS", text: $host, focus: .host,
                  placeholder: "100.113.110.31", keyboard: .URL)
            field("PORT", text: $port, focus: .port,
                  placeholder: "4242", keyboard: .numberPad)
            field("PAIRING CODE", text: $code, focus: .code,
                  placeholder: "7ZVT-Z9N5", keyboard: .asciiCapable)
            field("NAME THIS DEVICE", text: $label, focus: .label,
                  placeholder: "iPhone", keyboard: .default)
        }
    }

    private func field(_ title: String, text: Binding<String>, focus target: Field,
                       placeholder: String, keyboard: UIKeyboardType) -> some View {
        VStack(alignment: .leading, spacing: Space.xs) {
            Text(title)
                .font(OrcFont.label)
                .orcTracking(11)
                .foregroundStyle(Palette.textTertiary)
            TextField(placeholder, text: text)
                .font(OrcFont.code)
                .foregroundStyle(Palette.textPrimary)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .keyboardType(keyboard)
                .focused($focus, equals: target)
                .padding(Space.sm)
                .frame(minHeight: 44)
                .background(Palette.sunken)
                .clipShape(RoundedRectangle(cornerRadius: Radius.sm, style: .continuous))
                .overlay(
                    RoundedRectangle(cornerRadius: Radius.sm, style: .continuous)
                        .stroke(Palette.control, lineWidth: 1)
                )
        }
    }

    private var pairButton: some View {
        Button {
            guard let ticket else { return }
            Task { await store.pair(with: ticket, label: label) }
        } label: {
            Text(isPairing ? "pairing…" : "Pair")
                .font(OrcFont.button)
                .frame(maxWidth: .infinity, minHeight: 44)
        }
        // A disabled primary is NOT `.opacity(0.45)` — composited, that button
        // measures 2.22:1 and it is the first thing a user sees on this screen.
        // It is a `raised` fill with a `control` stroke and a `textTertiary`
        // label: 4.77:1, and still visibly disabled.
        .foregroundStyle(ticket == nil ? Palette.textTertiary : Palette.statusWorking)
        .background(ticket == nil ? Palette.raised : Palette.statusWorking.opacity(0.12))
        .clipShape(RoundedRectangle(cornerRadius: Radius.sm, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: Radius.sm, style: .continuous)
                .stroke(ticket == nil ? Palette.control : Palette.statusWorking, lineWidth: 1)
        )
        .disabled(ticket == nil || isPairing)
    }

    private var isPairing: Bool {
        if case .pairing = store.step { return true }
        return false
    }

    @ViewBuilder
    private var status: some View {
        if case .failed(let error) = store.step {
            VStack(alignment: .leading, spacing: Space.xs) {
                Text(headline(for: error))
                    .font(OrcFont.status)
                    .foregroundStyle(Palette.statusNeeds)
                Text(guidance(for: error))
                    .font(OrcFont.bodyCompact)
                    .foregroundStyle(Palette.textSecondary)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(Space.md)
            .background(
                RoundedRectangle(cornerRadius: Radius.sm, style: .continuous)
                    .stroke(Palette.statusNeeds.opacity(0.5), lineWidth: 1)
            )
        }
        if let warning = store.keychainWarning {
            Text(warning)
                .font(OrcFont.meta)
                .foregroundStyle(Palette.statusLimit)
        }
    }

    /// The pairing refusals get their own copy, because "409" is not a sentence
    /// and the server's own message is written for a terminal.
    private func headline(for error: OrchestraError) -> String {
        guard case .http(_, let refusal) = error, let code = refusal?.error else {
            return error.headline
        }
        switch code {
        case APIRefusal.Code.notOpen: return "no pairing window is open"
        case APIRefusal.Code.codeWrong: return "that isn't the code on the screen"
        case APIRefusal.Code.attempts, APIRefusal.Code.locked: return "too many tries"
        case APIRefusal.Code.peerRefused: return "this phone isn't on the tailnet"
        default: return error.headline
        }
    }

    private func guidance(for error: OrchestraError) -> String {
        guard case .http(_, let refusal) = error, let code = refusal?.error else {
            return error.guidance
        }
        switch code {
        case APIRefusal.Code.notOpen:
            // The window does not survive a restart — deliberately, because a
            // door that reopens by itself is a door nobody closed. The cost is
            // that this refusal also fires when the board was restarted
            // mid-pairing, which reads like a network fault unless it is named.
            return "It may have expired, been used already, or the board may have "
                 + "restarted. Open a new one on the Mac and scan again."
        case APIRefusal.Code.peerRefused:
            return "Pairing is answered on the Mac itself and on the tailnet only. "
                 + "Connect Tailscale on this phone first."
        default:
            return refusal?.message ?? error.guidance
        }
    }

    private static func defaultLabel() -> String {
        #if canImport(UIKit)
        return UIDevice.current.name
        #else
        return "iPhone"
        #endif
    }
}
