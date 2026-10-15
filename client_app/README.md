# eRecipe iOS client

A SwiftUI iPhone/iPad app and native Share extension, targeting iOS 17+. Open `eRecipe.xcodeproj` in Xcode. Client implementation lives in this directory; the application server is in `../server`.

## Run the demo

Choose the **eRecipe Demo** scheme and an iPhone simulator, then Run. The orange banner identifies demo mode. Tap **Paste recipe text**, enter some text, then **Share recipe**. The sheet closes as soon as the upload is accepted. The fixed Lemon chickpea salad fixture is processed and sent automatically; open Recent shares to inspect delivery. Launch with `--demo --demo-share` to exercise the extension’s shared-input screen without any button presses. No network service, model, or physical reader is contacted. Demo data is isolated from real registration and does not configure the Share extension.

The **eRecipe** scheme uses the real API. The app never falls back to demo responses on a network failure. Debug demo responses come from `Fixtures/`, copied from `server/docs/contract/examples/`; the PNG was rendered from that example's Markdown with the existing server renderer.

## Configure a real device

1. Create ignored `Config/Local.xcconfig` and set `DEVELOPMENT_TEAM = YOUR_TEAM_ID` there so the app and extension share the same team and project regeneration preserves it. Selecting a team only in Xcode writes it to the generated project and will be lost on regeneration. Override `ERECIPE_BUNDLE_PREFIX`, `ERECIPE_APP_GROUP`, and `ERECIPE_KEYCHAIN_GROUP` in the same file with identifiers registered under your Apple Developer team (see `Config/Signing.xcconfig` for defaults). The Keychain group must include `$(AppIdentifierPrefix)`.
2. Enable App Groups and Keychain Sharing for both targets, with matching group identifiers. App Group files contain source bodies and workflow state; only Keychain holds bearer credentials.
3. Deploy the server's application API at its Tailscale Serve **HTTPS origin**. The stage 1 LAN/firmware server is a different service and cannot register this client.
4. Install the app and connect Tailscale on the phone. Create an enrollment QR in the server dashboard and scan it with Camera. The installed app handles `erecipe://enroll?v=1&server=<encoded-origin>&code=<single-use-code>`, displays the destination, and redeems after confirmation.
5. In Notes, Safari, Files, or Photos, open the system Share sheet and enable **eRecipe** under More if needed. Choose eRecipe to upload immediately. The sheet closes automatically after the server accepts the upload and background delivery is queued, without waiting for extraction, rendering, or reader selection. When rendering finishes, page 1 is selected automatically; open Recent shares for progress, previews, and warnings.

The API contract is `server/docs/openapi.yaml` version `1.0.0-draft.3`. The integrated application server supports registration, text/image imports, extraction, previews, and publication; see `server/docs/SERVER_API.md` for deployment.

## Supported workflow

Sharing is now the user's authorization to send. This supersedes the earlier client plan's separate review/Send step. The client still calls the existing publication endpoint for an exact completed render set; server imports and renders themselves do not implicitly publish, and no new API fields are required.

- Registration validates the exact link shape, version, HTTPS origin, and canonical 32-byte base64url code. Redemption uses no existing bearer token and rejects redirects. Codes are never persisted or logged. A lost redemption response requires dashboard revocation of the created credential and a new QR; it is never automatically retried.
- The home screen shows the connection, readiness, and locally tracked shares. It includes text entry for testing and recovery; it is not a full shared-library browser.
- The Share extension accepts text, a UTF-8 `.txt` file, a website URL, or up to six still JPEG, PNG, or HEIC/HEIF recipe photos. Photos and Files use the same bounded image loader. The home screen also offers **Choose recipe photos**, with numbered selection order. Selecting photos follows the same automatic delivery flow as sharing from another app. Mixed image/text attachments and animated images are rejected.
- JPEG/PNG originals within upload limits are preserved. HEIC/HEIF is converted locally to JPEG; oversized photos are reduced to a maximum 4096-pixel dimension with orientation applied. Source files may be at most 30 MiB and 80 megapixels each; prepared uploads must fit the server’s 10 MiB/file, 30 MiB total, and 20 megapixel limits. The server receives JPEG/PNG only. Pages remain in selection/share order.
- For a simulator demo of the image flow, launch with `--demo --demo-image-share`. It imports a bundled PNG and uses the fixed demo extraction response, without contacting a model or reader.
- Import kinds and publication are gated by `/api/v1/status`, refreshed automatically every two seconds while the import screen is open. Connection recovery needs no manual refresh. The current server advertises text, images, and public recipe URLs.
- Share recipe links from Safari or another app, share a standalone HTTP(S) URL as text, or use **Paste recipe link**. URLs retain query parameters and fragments. The server fetches HTML and recipe metadata, then the client follows capture, extraction, rendering, and automatic delivery. Failed capture offers **Fetch link again**, preserving the recipe and using an idempotent new capture. Sites requiring login/JavaScript or returning unsupported content should be shared as text/photos instead.
- Launch with `--demo --demo-url-share` to exercise an already shared URL, including the capture job stage, without contacting a website, model, or reader.
- Uploads are saved as files before starting. App Group SQLite records preserve operation keys, account context, job references, preview identity, and publication receipts. App and extension use separate background session identifiers, with app callbacks for completed extension uploads. **Resume upload** reconciles an interrupted upload using its original key.
- Upload acceptance is tracked separately from processing success. The share sheet checks the local receipt every 250 ms and automatically dismisses after acknowledgement and background handoff; a large Dismiss button remains available if the host keeps it visible. Upload failures remain open with Retry upload, using the original operation key. Recent-share details refresh automatically every second, without a Refresh status button.
- Processing follows the returned job and its downstream jobs. Extraction success alone never triggers publication: a complete render set is required. Preview is tied to the exact rendered revision, with warnings and one-based page selection for viewing.
- Choosing eRecipe in the system Share sheet authorizes automatic publication of the resulting render set’s first page. The import begins without an additional confirmation button. An ambiguous send retries with the same key and page. A successful response means server selection, not proof of physical display; a later status check also detects another selected recipe.
- The upload, processing checks, and publication use file-backed background URLSession transfers. Each step is serialized into the transfer description, allowing the app to continue extension work after dismissal. Pending automatic shares resume on app activation. Retryable delivery failures use bounded backoff and the original publication key. iOS controls scheduling and may delay background checks substantially; force-quitting may require reopening the app. No bounded background completion time is promised.
- Disconnect attempts server revocation, cancels uploads, clears local records/bodies, and removes the token. If revocation fails, the UI distinguishes removing local data from revoking server access.

The deployment must serve `/api/v1` directly without redirects, including uploads. Foreground requests reject redirects; [iOS always follows redirects for background transfers](https://developer.apple.com/documentation/foundation/downloading-files-in-the-background). Do not configure cross-origin upload redirects. No ATS exceptions, analytics, or logging of request bodies/credentials are enabled.

## Implementation layout

- `App/`: SwiftUI entry point, connection/registration UI, recent shares, background-session handoff.
- `ShareExtension/`: extension activation and shared-input UI.
- `Shared/ImagePreparation.swift` and `Shared/SharedInputLoader.swift`: bounded file/provider loading, HEIC conversion, orientation, and upload validation.
- `App/RecipePhotoPicker.swift`: ordered system photo picker.
- `Shared/`: wire DTOs, URL validation, HTTP client, shared Keychain, transactional SQLite store, background uploads, processing/publishing, reusable UI.
- `Tests/`: portable core tests using the real contract examples and injected HTTP responses.
- `UITests/`: automatic upload dismissal, processing that stays queued, connection recovery, upload retry, and continued automatic publication of text, image, and URL inputs.
- `Scripts/generate_project.py`: deterministic standard-library-only generator for the checked-in project, plists, entitlements, and schemes. Rerun after adding source/resource files. It preserves `Signing.xcconfig` and local overrides.

## Verification

Verified October 14, 2026 with Xcode 27.0: 25 core tests and eight iPhone simulator UI tests passed. The app and Share extension built for the simulator. The UI suite covers automatic dismissal while processing remains queued, recovery from a connection failure without a refresh button, upload failure/retry, and continued delivery of pasted text, links, shared text, images, and URLs. Tests use mocked server responses; real Share extension provisioning, background transfer scheduling on a phone, and physical reader delivery still require device checks.

Core tests (no server or Apple signing required):

```sh
swift test --package-path client_app
```

Simulator build:

```sh
xcodebuild -project client_app/eRecipe.xcodeproj -scheme eRecipe \
  -configuration Debug -destination 'generic/platform=iOS Simulator' \
  -derivedDataPath /tmp/erecipe-derived CODE_SIGNING_ALLOWED=NO build
```

Simulator UI test (replace the destination with an installed simulator):

```sh
xcodebuild -project client_app/eRecipe.xcodeproj -scheme eRecipe \
  -destination 'platform=iOS Simulator,name=iPhone 18 Pro' \
  -derivedDataPath /tmp/erecipe-derived CODE_SIGNING_ALLOWED=NO test
```

Physical-device checks still required with the implemented server:

- Cold/warm registration, expired QR, lost redemption response, revoked credential, and disconnected Tailscale.
- Share extension activation and Keychain access from Notes, Files, Safari, and Photos with real provisioning.
- Dismiss/terminate the extension during upload, reopen the app, and confirm one import with the same operation key.
- Fail a processing stage, fix the server condition, and retry without duplicate recipes.
- Disconnect during an upload and confirm late callbacks cannot restore account data.
- Choose eRecipe in Share without tapping any further action; verify automatic TRMNL selection. Distinguish selection receipt, image-fetch evidence, and visually verified display.
- Confirm the share sheet closes after upload acceptance while processing is still pending, then verify background publication with the same request key.

Full library browsing, Markdown editing, and history/candidate management are outside this first client. Existing imports saved before automatic delivery was enabled are not published implicitly; share them again to send.
