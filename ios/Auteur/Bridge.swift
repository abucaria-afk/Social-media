import EventKit
import Photos
import SwiftUI
import UIKit
import WebKit

/// What the page asks the phone to do, and the answers.
///
/// Three jobs, and each one is here because the web layer on this platform
/// genuinely cannot do it rather than because native felt tidier:
///
/// * **saving a film.** A download link inside a web view does not reach the
///   camera roll; on iOS it opens the file in a viewer and leaves somebody to
///   work out the rest. This is the whole point of the film existing.
/// * **the share sheet.** `navigator.share` exists in Safari and not in a web
///   view, so the page's own share button would silently do nothing.
/// * **the calendar.** There is no web API for writing an event to the
///   calendar on the device.
///
/// Everything else the page already does for itself, including picking clips:
/// `<input type="file" accept="video/*,image/*">` opens the photo library
/// natively, so wrapping it would be a second implementation of something that
/// already works.
@MainActor
final class Bridge: NSObject, ObservableObject, WKScriptMessageHandler {
    @Published var note: String?

    private weak var view: WKWebView?

    func attach(_ view: WKWebView) { self.view = view }

    // MARK: - The page calling in

    nonisolated func userContentController(
        _ controller: WKUserContentController,
        didReceive message: WKScriptMessage
    ) {
        guard let payload = message.body as? [String: Any],
              let job = payload["job"] as? String else { return }
        Task { @MainActor in
            await handle(job: job, payload: payload)
        }
    }

    private func handle(job: String, payload: [String: Any]) async {
        switch job {
        case "save":
            await save(payload)
        case "share":
            share(payload)
        case "calendar":
            await addToCalendar(payload)
        case "capabilities":
            report()
        default:
            break
        }
    }

    // MARK: - Saving a film

    private func save(_ payload: [String: Any]) async {
        guard let data = decode(payload["data"]) else {
            say("That film could not be read.")
            return
        }
        let file = FileManager.default.temporaryDirectory
            .appendingPathComponent("auteur-\(UUID().uuidString).mp4")
        do {
            try data.write(to: file)
        } catch {
            say("Could not write the film to save it.")
            return
        }
        defer { try? FileManager.default.removeItem(at: file) }

        // `.addOnly` rather than full access: this app writes films and has no
        // reason to be able to read somebody's whole library. The picker the
        // page uses does not need this permission at all — iOS hands over only
        // what was chosen — so asking for read access would be asking for
        // something nothing here uses.
        let status = await PHPhotoLibrary.requestAuthorization(for: .addOnly)
        guard status == .authorized || status == .limited else {
            say("Saving needs permission to add to Photos. Settings › Auteur.")
            return
        }
        do {
            try await PHPhotoLibrary.shared().performChanges {
                PHAssetChangeRequest.creationRequestForAssetFromVideo(atFileURL: file)
            }
            say("Saved to Photos.")
        } catch {
            say("Photos would not take that film.")
        }
    }

    // MARK: - The share sheet

    private func share(_ payload: [String: Any]) {
        guard let data = decode(payload["data"]) else { return }
        let name = (payload["name"] as? String) ?? "film.mp4"
        let file = FileManager.default.temporaryDirectory.appendingPathComponent(name)
        guard (try? data.write(to: file)) != nil else { return }

        let sheet = UIActivityViewController(activityItems: [file], applicationActivities: nil)
        guard let host = view?.window?.rootViewController else { return }
        // An iPad presents this from a rectangle rather than from nowhere, and
        // without an anchor it raises rather than falling back to the middle.
        sheet.popoverPresentationController?.sourceView = view
        sheet.popoverPresentationController?.sourceRect = CGRect(
            x: (view?.bounds.midX ?? 0), y: (view?.bounds.maxY ?? 0) - 40, width: 1, height: 1
        )
        host.present(sheet, animated: true)
    }

    // MARK: - The calendar

    private func addToCalendar(_ payload: [String: Any]) async {
        let store = EKEventStore()
        let allowed: Bool
        if #available(iOS 17.0, *) {
            allowed = (try? await store.requestWriteOnlyAccessToEvents()) ?? false
        } else {
            allowed = await withCheckedContinuation { keep in
                store.requestAccess(to: .event) { granted, _ in keep.resume(returning: granted) }
            }
        }
        guard allowed else {
            say("Adding a shoot needs permission for your calendar.")
            return
        }
        guard let stamp = payload["when"] as? String,
              let start = ISO8601DateFormatter().date(from: stamp) else {
            say("That plan has no date on it yet.")
            return
        }

        let event = EKEvent(eventStore: store)
        event.title = (payload["title"] as? String) ?? "Shoot"
        event.notes = payload["notes"] as? String
        event.startDate = start
        event.endDate = start.addingTimeInterval(15 * 60)
        event.calendar = store.defaultCalendarForNewEvents
        // The same three moments the subscribable feed carries, so the app and
        // the calendar subscription do not disagree about when to nudge you.
        event.addAlarm(EKAlarm(relativeOffset: -48 * 3600))
        event.addAlarm(EKAlarm(relativeOffset: -12 * 3600))
        event.addAlarm(EKAlarm(relativeOffset: 0))
        do {
            try store.save(event, span: .thisEvent)
            say("Added to your calendar.")
        } catch {
            say("The calendar would not take that.")
        }
    }

    // MARK: - What this device can actually do

    /// The renderer needs `canvas.captureStream` and `MediaRecorder`, and web
    /// views have not always had both. Rather than let a render fail with
    /// nothing on screen, the page asks on launch and this answers honestly.
    private func report() {
        let ios = UIDevice.current.systemVersion
        view?.evaluateJavaScript(
            "window.dispatchEvent(new CustomEvent('auteur-native',{detail:{ios:'\(ios)'}}))"
        )
    }

    // MARK: - Bits

    private func decode(_ value: Any?) -> Data? {
        guard let text = value as? String else { return nil }
        // A data: URL or bare base64, whichever the page sent.
        let body = text.contains(",") ? String(text.split(separator: ",", maxSplits: 1)[1]) : text
        return Data(base64Encoded: body, options: .ignoreUnknownCharacters)
    }

    private func say(_ message: String) {
        note = message
        Task {
            try? await Task.sleep(nanoseconds: 2_600_000_000)
            if note == message { note = nil }
        }
    }
}

extension Bridge {
    /// The shim injected before the page runs. Kept in `native.js` and read
    /// from the bundle rather than pasted in here as a Swift string literal:
    /// a copy in two languages is a copy that goes stale, and this one is
    /// edited far more often than the Swift around it.
    static var shim: String {
        guard let url = Bundle.main.url(forResource: "native", withExtension: "js"),
              let source = try? String(contentsOf: url, encoding: .utf8) else {
            assertionFailure("native.js is missing from the bundle")
            return ""
        }
        return source
    }
}
