import UIKit
import SwiftUI

final class ShareViewController: UIViewController {
    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground
        Task { @MainActor [weak self] in
            guard let self else { return }
            do {
                let environment = try ClientEnvironment()
                let input = try await SharedInputLoader.load(extensionContext?.inputItems ?? [])
                let content = NavigationStack {
                    ImportView(environment: environment, initialInput: input) { [weak self] in
                        self?.extensionContext?.completeRequest(returningItems: nil)
                    }
                }
                show(content)
            } catch {
                show(NavigationStack {
                    VStack(spacing: 20) {
                        ContentUnavailableView("Unable to share", systemImage: "exclamationmark.triangle", description: Text(error.localizedDescription))
                        Button("Done") { [weak self] in self?.extensionContext?.completeRequest(returningItems: nil) }
                    }.padding()
                })
            }
        }
    }
    @MainActor private func show<V: View>(_ content: V) {
        let host = UIHostingController(rootView: content)
        addChild(host); host.view.translatesAutoresizingMaskIntoConstraints = false; view.addSubview(host.view)
        NSLayoutConstraint.activate([host.view.leadingAnchor.constraint(equalTo: view.leadingAnchor), host.view.trailingAnchor.constraint(equalTo: view.trailingAnchor), host.view.topAnchor.constraint(equalTo: view.topAnchor), host.view.bottomAnchor.constraint(equalTo: view.bottomAnchor)])
        host.didMove(toParent: self)
    }
}

