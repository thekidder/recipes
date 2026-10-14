import SwiftUI

@main
struct ERecipeApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) var delegate
    var body: some Scene {
        WindowGroup {
            if let environment = delegate.environment { HomeView(environment: environment) }
            else { ContentUnavailableView("Setup unavailable", systemImage: "exclamationmark.triangle", description: Text(delegate.failure ?? "Check app configuration.")) }
        }
    }
}

final class AppDelegate: NSObject, UIApplicationDelegate {
    let environment: ClientEnvironment?
    let failure: String?
    override init() {
        do {
            #if DEBUG
            environment = try ClientEnvironment(demo: ProcessInfo.processInfo.arguments.contains("--demo"))
            #else
            environment = try ClientEnvironment()
            #endif
            failure = nil
        } catch { environment = nil; failure = error.localizedDescription }
        super.init()
    }
    func application(_ application: UIApplication, handleEventsForBackgroundURLSession identifier: String,
                     completionHandler: @escaping () -> Void) {
        guard let environment else { completionHandler(); return }
        environment.uploads.reconnect(identifier, completion: completionHandler)
    }
}
