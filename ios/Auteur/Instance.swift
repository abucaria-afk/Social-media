import SwiftUI

/// Where this app's server is, if it has one.
///
/// The app ships with the whole edit room inside it and works with the phone
/// in aeroplane mode — that is the default and it needs nothing. But the
/// bundled page is only the part of the app that can run alone: the feed, the
/// messages and the manager all need the instance that holds them, because a
/// feed of films other people made cannot exist inside one phone.
///
/// So this is not a compromise on "nothing leaves your phone" — it is what
/// that sentence actually means. There is no service here. The address is one
/// you typed, pointing at a copy of `auteur serve` you are running, usually on
/// your own wifi. Nothing is sent anywhere else, and with no address set,
/// nothing is sent anywhere at all.
@MainActor
final class Instance: ObservableObject {
    private static let key = "auteur.instance"

    @Published private(set) var address: String

    init() {
        address = UserDefaults.standard.string(forKey: Self.key) ?? ""
    }

    var isSet: Bool { !address.isEmpty }

    /// The page to load: the instance if there is one, the bundle otherwise.
    var start: URL? {
        if let url = URL(string: address), url.scheme != nil, url.host != nil {
            return url
        }
        return Bundle.main.url(forResource: "index", withExtension: "html", subdirectory: "Web")
    }

    /// Accepts what somebody would actually type, and says no to the rest.
    ///
    /// `192.168.1.20:8000` is what people read off the terminal, so a missing
    /// scheme is filled in rather than refused. A scheme that is not http is,
    /// because this is only ever pointed at a copy of the server.
    func remember(_ typed: String) -> String? {
        var text = typed.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else {
            forget()
            return nil
        }
        if !text.contains("://") { text = "http://" + text }
        guard let url = URL(string: text),
              let scheme = url.scheme?.lowercased(),
              scheme == "http" || scheme == "https",
              url.host != nil else {
            return "That does not look like an address. It should read something "
                + "like 192.168.1.20:8000."
        }
        address = url.absoluteString
        UserDefaults.standard.set(address, forKey: Self.key)
        return nil
    }

    func forget() {
        address = ""
        UserDefaults.standard.removeObject(forKey: Self.key)
    }
}

/// Asked once, and reachable again from the bundled page.
struct ConnectSheet: View {
    @ObservedObject var instance: Instance
    @Binding var showing: Bool
    @State private var typed: String = ""
    @State private var problem: String?

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("192.168.1.20:8000", text: $typed)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .keyboardType(.URL)
                } header: {
                    Text("Your Auteur")
                } footer: {
                    Text(
                        "Run `auteur serve` on your computer and it prints this "
                        + "address. Both devices need the same wifi. Leave it empty "
                        + "to use the app on its own — it still cuts films, it just "
                        + "has no feed and no messages, because those live on the "
                        + "instance rather than on the phone."
                    )
                }

                if let problem {
                    Text(problem).foregroundStyle(.red)
                }

                if instance.isSet {
                    Section {
                        Button("Disconnect", role: .destructive) {
                            instance.forget()
                            typed = ""
                            showing = false
                        }
                    } footer: {
                        Text("The app goes back to working entirely on this phone.")
                    }
                }
            }
            .navigationTitle("Connect")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Not now") { showing = false }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Connect") {
                        problem = instance.remember(typed)
                        if problem == nil { showing = false }
                    }
                }
            }
            .onAppear { typed = instance.address }
        }
    }
}
