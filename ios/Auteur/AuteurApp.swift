import SwiftUI

/// The whole app: one screen, which is the edit room.
///
/// The editing itself runs in a web view loading a page out of the bundle —
/// the same renderer the desktop program uses, so a cut made on the phone and
/// a cut made at a desk are the same code rather than two implementations that
/// drift. What is native is everything the web layer genuinely cannot do on
/// this platform: reaching the photo library to save a finished film, the
/// system share sheet, and writing a shoot into the calendar.
@main
struct AuteurApp: App {
    var body: some Scene {
        WindowGroup {
            EditRoom()
                // The page paints under the notch and the home indicator and
                // puts its own controls back inside the safe area, the same
                // way it does in a browser.
                .ignoresSafeArea()
                .preferredColorScheme(nil)  // follow the phone
        }
    }
}

struct EditRoom: View {
    @StateObject private var bridge = Bridge()
    @StateObject private var instance = Instance()
    @State private var connecting = false

    var body: some View {
        WebHost(bridge: bridge, instance: instance)
            .sheet(isPresented: $connecting) {
                ConnectSheet(instance: instance, showing: $connecting)
            }
            // Only from the bundled page: connected, the app's own settings
            // screen is on the instance and this button would be a second one
            // saying something different.
            .overlay(alignment: .topTrailing) {
                if !instance.isSet {
                    Button {
                        connecting = true
                    } label: {
                        Image(systemName: "antenna.radiowaves.left.and.right")
                            .padding(10)
                            .background(.thinMaterial, in: Circle())
                    }
                    .padding(.top, 54)
                    .padding(.trailing, 14)
                    .accessibilityLabel("Connect to your Auteur")
                }
            }
            .overlay(alignment: .bottom) {
                if let note = bridge.note {
                    Text(note)
                        .font(.footnote)
                        .padding(.horizontal, 14)
                        .padding(.vertical, 10)
                        .background(.thinMaterial, in: Capsule())
                        .padding(.bottom, 34)
                        .transition(.move(edge: .bottom).combined(with: .opacity))
                }
            }
            .animation(.easeOut(duration: 0.2), value: bridge.note)
    }
}
