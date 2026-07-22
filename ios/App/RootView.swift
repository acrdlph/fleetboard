import SwiftUI

/// Paired or not. There is no third state worth a screen: with no token there is
/// nothing to fetch and no request worth making.
struct RootView: View {
    @Bindable var model: AppModel

    var body: some View {
        Group {
            if model.pairing.isPaired {
                FleetView(store: model.fleet,
                          serverLabel: model.pairing.profile?.display ?? "—") {
                    Task { await model.unpair() }
                }
            } else {
                PairingScreen(store: model.pairing)
            }
        }
        .background(Palette.canvas)
        .onOpenURL { url in
            // The pairing QR is `orc://p?h=…&p=…&c=…`, so scanning it with the
            // SYSTEM camera opens the app straight here. Same ticket, same
            // claim path, no second implementation.
            //
            // It only fires when the phone is UNPAIRED. A link is something
            // anything on the phone can hand us, and "already paired" is the
            // state where accepting one silently would replace a working server
            // with one somebody else chose. Unpaired, the worst a bad link can
            // do is claim a code it does not have.
            guard !model.pairing.isPaired,
                  let ticket = PairingTicket(url: url.absoluteString) else { return }
            Task { await model.pairing.pair(with: ticket, label: AppModel.deviceLabel) }
        }
    }
}
