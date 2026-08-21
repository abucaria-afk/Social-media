import SwiftUI
import WebKit

/// The web view, and the rules it runs under.
struct WebHost: UIViewRepresentable {
    let bridge: Bridge

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

        // From the bundle, with the folder granted so nothing has to be
        // inlined that does not need to be. There is no network entitlement in
        // this app at all; a page that reached outside would simply not load,
        // which is why the build script refuses to ship one that does.
        if let page = Bundle.main.url(forResource: "index", withExtension: "html", subdirectory: "Web") {
            view.loadFileURL(page, allowingReadAccessTo: page.deletingLastPathComponent())
        }
        return view
    }

    func updateUIView(_ view: WKWebView, context: Context) {}
}
