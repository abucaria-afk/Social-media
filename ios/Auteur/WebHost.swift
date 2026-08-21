import SwiftUI
import WebKit

/// The web view, and the rules it runs under.
struct WebHost: UIViewRepresentable {
    let bridge: Bridge
    @ObservedObject var instance: Instance

    func makeUIView(context: Context) -> WKWebView {
        let settings = WKWebViewConfiguration()

        // Video has to play where it sits. Without this, tapping a clip throws
        // the phone into the fullscreen player and the app disappears behind
        // it — the same reason `playsinline` is on every video tag in the page.
        settings.allowsInlineMediaPlayback = true
        settings.mediaTypesRequiringUserActionForPlayback = []

        // The bridge. One handler name, with the message saying which job it
        // is: several handlers would each need registering, unregistering and
        // remembering, and the failure when one is missed is silence.
        let controller = WKUserContentController()
        controller.add(bridge, name: "auteur")
        controller.addUserScript(
            WKUserScript(
                source: Bridge.shim,
                injectionTime: .atDocumentStart,
                forMainFrameOnly: true
            )
        )
        settings.userContentController = controller

        let view = WKWebView(frame: .zero, configuration: settings)
        view.isOpaque = false
        view.backgroundColor = .clear
        view.scrollView.contentInsetAdjustmentBehavior = .never
        // The page is an app, not a document: rubber-banding past the top of
        // it reads as the page having come loose.
        view.scrollView.bounces = false
        #if DEBUG
        if #available(iOS 16.4, *) { view.isInspectable = true }
        #endif

        bridge.attach(view)

        load(into: view)
        return view
    }

    func updateUIView(_ view: WKWebView, context: Context) {
        // Connecting or disconnecting changes which page this is.
        let wanted = instance.start
        if view.url?.absoluteString != wanted?.absoluteString {
            load(into: view)
        }
    }

    /// The instance if one is set, and the bundle otherwise.
    ///
    /// The bundled page is loaded as a file URL with its folder granted, so it
    /// works with no network at all. An instance is an ordinary load — the
    /// feed and the messages live there because a feed of other people's films
    /// cannot exist inside one phone.
    private func load(into view: WKWebView) {
        guard let start = instance.start else { return }
        if start.isFileURL {
            view.loadFileURL(start, allowingReadAccessTo: start.deletingLastPathComponent())
        } else {
            view.load(URLRequest(url: start))
        }
    }
}
