import SwiftUI

struct HomeView: View {
    let environment: ClientEnvironment
    @Environment(\.scenePhase) private var scenePhase
    @State private var connection: Connection?
    @State private var status: ServerStatus?
    @State private var shares: [PendingShare] = []
    @State private var enrollment: EnrollmentLink?
    @State private var showEnrollment = false
    @State private var showDisconnect = false
    @State private var localRemoval = false
    @State private var error: String?
    @State private var busy = false
    @State private var showImport = false
    @State private var importKind = "text"
    @State private var openedDemoShare = false
    private var demoSharedInput: ShareInput? {
        #if DEBUG
        if environment.demo && ProcessInfo.processInfo.arguments.contains("--demo-url-share") {
            return ShareInput(kind: "url", url: "https://example.com/lemon-salad?source=share#recipe")
        }
        if environment.demo && ProcessInfo.processInfo.arguments.contains("--demo-image-share"),
           let url = Bundle.main.url(forResource: "demo-preview", withExtension: "png"),
           let data = try? Data(contentsOf: url), let image = try? ImagePreparation.prepare(data, index: 1) {
            return ShareInput(kind: "images", images: [image])
        }
        if environment.demo && ProcessInfo.processInfo.arguments.contains("--demo-share") {
            return ShareInput(kind: "text", text: "Lemon chickpea salad. Mix chickpeas with lemon juice. Serve chilled.")
        }
        #endif
        return nil
    }
    @State private var showPhotos = false
    @State private var photoInput: ShareInput?
    @State private var loadingPhotos = false

    var body: some View {
        NavigationStack {
            homeList
            .navigationTitle("eRecipe")
            .refreshable { await refresh() }
            .onOpenURL { url in
                guard !busy else { error = "Finish the current operation before opening another registration link."; return }
                do {
                    guard connection == nil else { throw ClientError("Disconnect the current account before registering another one.") }
                    enrollment = try EnrollmentLink(url); showEnrollment = true
                } catch { self.error = error.localizedDescription }
            }
            .alert("Connect to this server?", isPresented: $showEnrollment) {
                Button("Connect") { redeem() }
                Button("Cancel", role: .cancel) { enrollment = nil }
            } message: { Text(enrollment?.origin.absoluteString ?? "") }
            .confirmationDialog("Disconnect from eRecipe? Pending local shares will be removed.", isPresented: $showDisconnect, titleVisibility: .visible) {
                Button("Revoke credential and disconnect", role: .destructive) { disconnect(localOnly: false) }
            }
            .confirmationDialog("Server revocation could not be confirmed. Removing this device’s local data does not revoke its server credential.", isPresented: $localRemoval, titleVisibility: .visible) {
                Button("Remove local data anyway", role: .destructive) { disconnect(localOnly: true) }
            }
            .sheet(isPresented: $showImport, onDismiss: { Task { await refresh() } }) { [importKind, photoInput] in
                NavigationStack { ImportView(environment: environment, initialInput: photoInput ?? demoSharedInput, entryKind: importKind, finish: { showImport = false }) }
            }
            .sheet(isPresented: $showPhotos, onDismiss: {
                if photoInput != nil { showImport = true }
            }) {
                RecipePhotoPicker(completion: pickedPhotos).interactiveDismissDisabled(loadingPhotos)
                    .overlay {
                        if loadingPhotos {
                            ProgressView("Preparing photos…").padding().background(.regularMaterial, in: RoundedRectangle(cornerRadius: 12))
                        }
                    }
            }

            .task(id: scenePhase) {
                guard scenePhase == .active else { return }
                if demoSharedInput != nil && !openedDemoShare { openedDemoShare = true; showImport = true }
                await environment.uploads.resumePending()
                while !Task.isCancelled {
                    await refresh()
                    do { try await Task.sleep(nanoseconds: 5_000_000_000) } catch { return }
                }
            }
        }
    }
    private var homeList: some View {
            List {
                if environment.demo {
                    Section { Label("Demo — no server or reader is contacted", systemImage: "testtube.2").foregroundStyle(.orange) }
                }
                if let connection {
                    Section("Connection") {
                        Label(connection.user.displayName, systemImage: "person.crop.circle")
                        Text(connection.origin.host ?? "Server").font(.footnote).foregroundStyle(.secondary)
                        if let status {
                            Label(status.readiness.isReady ? "Ready for recipes" : "Server needs attention", systemImage: status.readiness.isReady ? "checkmark.circle" : "exclamationmark.triangle")
                            ForEach(status.readiness.issues, id: \.code) { Text($0.message).font(.footnote) }
                        }
                        Button("Disconnect", role: .destructive) { showDisconnect = true }.disabled(busy)
                    }
                    Section {
                        Button { photoInput = nil; importKind = "text"; showImport = true } label: { Label("Paste recipe text", systemImage: "text.badge.plus") }
                            .disabled(loadingPhotos)
                        if status?.capabilities.importKinds.contains("url") == true {
                            Button { photoInput = nil; importKind = "url"; showImport = true } label: { Label("Paste recipe link", systemImage: "link") }
                                .disabled(loadingPhotos || busy)
                        }
                        if status?.capabilities.importKinds.contains("images") == true {
                            Button { photoInput = nil; showPhotos = true } label: { Label("Choose recipe photos", systemImage: "photo.on.rectangle.angled") }
                                .disabled(loadingPhotos || busy)
                        }
                        if loadingPhotos { ProgressView("Preparing photos…") }
                        Text("From another app, tap Share and choose eRecipe. Your recipe is sent to your reader automatically when ready.").font(.footnote).foregroundStyle(.secondary)
                    }
                    Section("Recent shares") {
                        if shares.isEmpty { Text("Your shared recipes will appear here.").foregroundStyle(.secondary) }
                        ForEach(shares) { item in
                            NavigationLink {
                                RecipeFlowView(environment: environment, id: item.id)
                            } label: {
                                VStack(alignment: .leading, spacing: 5) {
                                    Text(item.revision?.title ?? item.summary).lineLimit(2)
                                    Text(item.phase.capitalized).font(.caption).foregroundStyle(.secondary)
                                }
                            }
                            .accessibilityIdentifier("recent-share")
                        }
                    }
                } else {
                    Section {
                        ContentUnavailableView("Connect your recipe reader", systemImage: "qrcode", description: Text("Install and connect Tailscale, then scan the registration QR code from your eRecipe server dashboard using Camera."))
                    }
                }
                if let error { Section { Text(error).foregroundStyle(.red).font(.callout) } }
            }
    }
    private func pickedPhotos(_ providers: [NSItemProvider]) {
        guard !loadingPhotos else { return }
        photoInput = nil
        guard !providers.isEmpty else { showPhotos = false; return }
        loadingPhotos = true
        let context = connection?.context
        Task { @MainActor in
            do {
                let input = try await SharedInputLoader.loadProviders(providers)
                guard context == connection?.context else { throw ClientError("The connection changed. Choose your photos again.") }
                photoInput = input
            } catch { self.error = error.localizedDescription }
            loadingPhotos = false
            showPhotos = false
        }
    }
    @MainActor private func refresh() async {
        guard !busy else { return }
        do {
            connection = try environment.store.connection()
            if let connection {
                shares = try environment.store.items(context: connection.context)
                status = try await environment.api(for: connection).status(for: connection)
            }
            error = nil
        } catch { self.error = error.localizedDescription; status = nil }
    }
    private func redeem() {
        guard let invitation = enrollment else { return }
        enrollment = nil; busy = true
        Task { @MainActor in
            do {
                let receipt: EnrollmentReceipt = try await APIClient(origin: invitation.origin).post("/api/v1/enrollments/redeem", body: ["code": invitation.code])
                guard receipt.tokenType == "Bearer" else { throw ClientError("Unsupported server credential type.") }
                let newConnection = Connection(origin: invitation.origin, serverId: receipt.serverId, user: receipt.user, credentialId: receipt.credential.id)
                try environment.vault.save(receipt.accessToken, for: newConnection)
                try environment.store.setConnection(newConnection)
                connection = newConnection
                status = try await environment.api(for: newConnection).status(for: newConnection)
                error = nil
            } catch {
                self.error = error.localizedDescription + " If the server consumed the registration code without saving a credential here, revoke the linked credential in the dashboard and request a new QR."
            }
            busy = false
        }
    }
    private func disconnect(localOnly: Bool) {
        guard let connection else { return }
        busy = true
        Task { @MainActor in
            do {
                if !localOnly && !connection.isDemo {
                    do { _ = try await environment.api(for: connection).data("/api/v1/credentials/current/revoke", method: "POST") }
                    catch let problem as APIError where problem.error.code == "invalid_credentials" || problem.error.code == "user_disabled" { /* already invalid */ }
                }
                if !connection.isDemo { await environment.uploads.cancelAll() }
                try environment.vault.remove(connection)
                try environment.store.clear()
                self.connection = nil; shares = []; status = nil; error = nil
            } catch { self.error = error.localizedDescription; if !localOnly { localRemoval = true } }
            busy = false
        }
    }
}
