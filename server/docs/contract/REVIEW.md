# eRecipe API contract guide

Contract `1.0.0` · October 13, 2026 · **Server implemented; deployment and iOS integration pending**

[openapi.yaml](../openapi.yaml) is the authoritative wire contract. Its schemas, operation descriptions, and `x-contract` rules together define the interface. This document explains the decisions and implementation dependencies; it is not a second specification. The earlier handoff's API section is a seed superseded by this contract. Its product requirements still apply.

The server implementation is in `server/api/` and the integrated launcher. See [setup and migration](../SERVER_API.md) and [verification results](../../results/API_IMPLEMENTATION.md). Processing-owned source files remain unchanged. The 37 synthetic exchanges in [examples/index.json](examples/index.json) are independently usable mock scenarios; runtime coverage is recorded separately.

## Enrollment and access

The dashboard and `/control/v1` are accessible through **Tailscale Serve over HTTPS**, using the same configured origin as the iOS API. **Anyone permitted to reach this service by the tailnet access rules is trusted to administer it.** There is no dashboard login, application-level administrator allowlist, or required Tailscale user-identity header. All active application users still have equal shared-library access.

Serve proxies to a dedicated loopback-bound application listener, separate from the reader's LAN listener. Direct LAN requests cannot access the administrative routes by supplying proxy headers. The local host remains trusted. Both listeners must share coordinated storage access rather than starting independent server writers. Existing direct-loopback legacy controls can retain their local protections; any legacy controls served remotely must use the same ingress and browser request protections as the dashboard.

Dashboard POST requests require `X-eRecipe-Control: 1` and an `Origin` matching the configured Serve HTTPS origin. Host validation also uses that configured origin, rather than the internal HTTP upstream address. These checks preserve browser request protections behind the proxy. The iOS API continues to require its enrolled bearer token. OpenAPI `security: []` on dashboard routes means no application credential; tailnet reachability remains required.

Tailscale documents Serve access control and backend guidance in [Tailscale Serve](https://tailscale.com/docs/features/tailscale-serve). A tagged device or a request without user-identity headers can use the dashboard if the tailnet rules permit access. Identity headers may provide optional audit context, but never determine dashboard authorization. The actual Serve hostname is deployment configuration; no administrator login list is needed. This contract does not enable a public Funnel endpoint.

1. Create a named user on the dashboard. This creates an identity, without a device credential.
2. Select **Connect iPhone** and enter a device label. The server creates an enrollment valid for **10 minutes** and displays a QR code in the authorized dashboard.
3. The QR contains `erecipe://enroll?v=1&server=<encoded-HTTPS-origin>&code=<single-use-code>`. It contains no permanent bearer token. Its server URL is the configured Serve HTTPS origin, separate from the reader's LAN image origin and the loopback upstream address.
4. The installed app opens the link, validates its fields, shows the server address, and obtains the user's confirmation. It sends the code in a JSON body to `POST /api/v1/enrollments/redeem`, without an existing bearer token and without following redirects.
5. The server atomically consumes the code and creates one device credential. The response contains the user, credential ID, stable server ID, and permanent token.
6. The app saves the token in Keychain and the server URL/server ID in local configuration. It checks `/api/v1/status` and loads the shared library. The dashboard polls enrollment status and dismisses the QR after redemption.

Codes and tokens use 32 random bytes encoded as unpadded base64url. The server stores hashes only; secret-bearing responses are never cached or logged. Tokens are valid until revoked. Disabling a user invalidates all their credentials and pending enrollments, while preserving authorship and shared content. A new device gets its own credential.

One pending QR per user is allowed. Issuing a replacement invalidates the previous pending QR; it does not revoke already enrolled devices. The dashboard can revoke individual device credentials. Signing out should attempt server revocation before clearing local credentials; clearing local storage alone is not server revocation.

**Lost-response decision:** a consumed code is never replayed, even if the phone misses the token response. The dashboard's enrollment status identifies the credential that was created. The operator revokes it and issues a new QR. This deliberately avoids storing a recoverable permanent token or adding a second secret-exchange protocol. Ordinary recipe operations do have retry-safe responses.

An expired/revoked/used code returns `410 enrollment_unavailable`; an unknown well-formed code returns `401 enrollment_invalid`. Redemption is rate-limited to 20 requests per source per minute and 100 globally, with `Retry-After` on `429`.

The phone needs the app installed and a working Tailscale connection. The deployed HTTPS hostname and app URL-scheme registration remain deployment inputs. Custom URL schemes are not exclusive to one app; the QR flow does not authenticate the identity of the installed handler. Apple describes URL-scheme registration and incoming-link handling in [Defining a custom URL scheme](https://developer.apple.com/documentation/xcode/defining-a-custom-url-scheme-for-your-app). Universal Links and installation/deferred-link flows are outside v1.

## Application behavior

| Action | Contract result |
| --- | --- |
| Import text | `202` only after original text and extraction job are durably committed |
| Track import | Stage job includes results and downstream job IDs; extraction success can still mean rendering is pending |
| Review | Recipe returns current Markdown, candidate count, exact current-preview state, and links to paginated evidence/history |
| Edit | New immutable Markdown revision, incremented lock version, and render job; no extraction or automatic publication |
| Re-extract | Uses saved evidence and creates a candidate if a current revision exists |
| Refresh URL | Capability disabled; once enabled, creates a new linked capture while preserving existing evidence and edits |
| Accept candidate | Explicit current-pointer update with conflict check; reuses or queues its render |
| Render | Exact revision/profile; complete cache hit returns `200`, queued/running work returns `202` |
| Send to reader | Explicit render set and one-based page; `201` confirms server selection only |
| Archive | Retains all evidence, stops active work, and leaves the reader selection unchanged |

Supported input is text and still JPEG/PNG images. URL request schemas are fixed here but URL remains absent from `capabilities.import_kinds` until its acquisition path is ready. Unsupported input returns a structured error before creating a recipe. Missing OpenAI configuration is different: preserve a supported import and expose the configuration failure through readiness/jobs so it can be retried.

The first supported render profile is `trmnl-x-landscape-v1`: 1872 × 1404, one-bit PNG, empty preferences. Pages remain an ordered array even while the renderer produces one page. Overflow must fail visibly; the renderer cannot crop or omit recipe content.

Markdown is the only editable document. Title is derived from it. All revisions retain source association. A pending candidate is an extraction result that has never been accepted; an old accepted revision does not become a candidate when a newer revision is saved. Historical restore and candidate dismissal are deferred.

Publication targets any retained complete revision or candidate explicitly selected by the user. The most recent committed distinct send wins. Replaying an older send's idempotency key returns its original selection receipt without selecting that image again. Device responses distinguish selected publication, image-fetch evidence, and unavailable physical-display evidence. A device may use a cached image, so missing fetch evidence is not proof of a failed display.

## Request, retry, and cache rules

- Ordinary mutating commands require a client-generated UUID `Idempotency-Key`. The same user/key/operation/target/payload returns the original successful status, body, and resource links. Changed payload or target returns `409 idempotency_conflict`.
- Receipts persist with the records and commit atomically with the mutation. Replay happens before stale-version/state checks, after authentication. Internal worker keys use a separate namespace. Enrollment secret creation/redemption and current-credential revocation are documented exceptions.
- Edits, acceptance, and archive require `expected_lock_version`. A stale version returns `409 revision_conflict` with current revision ID and lock version. The app keeps its draft and asks the user to reconcile. Render/job progress does not increment the editing lock.
- All failures use `error.code`, `message`, `retryable`, `details`, plus a request ID. Codes are extensible. Markdown violations can include a one-based line number. Framework errors must also use this envelope.
- Collections use an opaque cursor, default page size 50, maximum 100, and a stable ID tie-breaker. Cursor expiry is 24 hours. A cursor binds the query filters, page size, target, and first-page high-water bound.
- Cache reconciliation traverses `GET /recipes?visibility=all`, including explicit archive records. The list is ordered by immutable creation time and ID; it is not an incremental sync feed or snapshot. Never delete a cached record merely because it was absent from one traversal. Later refreshes revisit mutable records and newly created recipes.
- JSON responses use `no-store`. Immutable assets use authenticated private caching and a full SHA-256 ETag; authentication runs before a `304`. Cache identity includes server/access context. HTML and provenance downloads are attachments; source markup is never executable application content.

## Input limits

| Input | v1 limit |
| --- | --- |
| JSON HTTP body | 1 MiB, including JSON encoding |
| Text or edited Markdown | 100,000 Unicode scalar values; non-whitespace text required |
| URL | 8,192 Unicode scalar values; preserve exact submitted string |
| Images | 6 still JPEG/PNG files, 10 MiB each, 30 MiB summed file bytes |
| Multipart envelope | 31 MiB total; JSON metadata at most 16 KiB |
| Image dimensions | At most 20 million decoded pixels per image |

Multipart uses an `application/json` part named `metadata` and file parts named `image_1` through `image_6`. The metadata attachment array defines order; each entry refers to exactly one unique file part. Missing, duplicated, unlisted, or additional parts are rejected. Byte signatures must match the declared image type. HEIC, animated images, PDF, and SVG are unsupported. A phone may convert before submission, but the server's preserved original is then exactly the submitted converted bytes.

Public HTTP(S) recipe URLs must meet the processing pipeline's network restrictions, including redirect/subresource protections. The enrollment origin is intentionally a different policy: it identifies the private application server.

## Processing integration

The API adapter composes these operations with the existing processing library. It adds API tables and transaction handling without changing processing-owned files.

| Required domain capability | Why the API needs it |
| --- | --- |
| Create identity without token; issue/revoke credentials; disable user | Dashboard account lifecycle and per-device enrollment |
| Atomic enrollment consumption and linked credential creation | Single-use codes remain single-use under races and restart |
| Stable persisted server ID and configured application HTTPS origin | Correct QR links and cache partitioning |
| Trusted Serve ingress governed by tailnet access rules | Remote dashboard access without an application login or forged LAN ingress |
| Durable idempotency receipts beyond jobs | Edits, acceptance, archive, dashboard administration, and publication retries |
| Explicit acceptance history | Distinguish pending extraction candidates from old accepted revisions |
| Domain errors with stable code/details | Conflict payloads and useful client recovery without parsing exception strings |
| Job-result lookup, downstream edges, atomic chained cancellation | Reliable workflow polling and cancellation at stage boundaries |
| Transactional archive check in all mutations/job completion | Prevent accepted work from reappearing after archive |
| Atomic publication plus device-state selection | Preserve legacy BYOS behavior and last-explicit-send semantics |
| Bounded listing/read projections | Avoid exposing SQL rows, secrets, storage paths, or unbounded histories |

`ApiLibrary` supplies composable transactions, durable receipts, acceptance tracking, and exact render results. Explicit DTO projections map source/provenance records to this contract. `DeviceStore` imports legacy JSON device state into SQLite and commits publication and reader selection together. Existing firmware and local-control tests remain passing. Historical acceptances without surviving pointer/base evidence cannot be reconstructed perfectly from pre-API data; see migration notes.

## Validation and review status

The contract has 35 paths, 37 operations, and 62 component schemas. The validation script checks OpenAPI structure, local references, every example against its operation's request/response schema, required idempotency headers, enrollment-link structure, error/request-ID agreement, and selected invalid payloads. The examples contain fictional IDs, a reserved invalid hostname, fake secrets, and synthetic asset descriptors; no live credentials, provider calls, image files, or device sends were used.

Run validation in an isolated environment, without changing the server's dependencies:

```sh
python3 -m venv /tmp/erecipe-contract-check
/tmp/erecipe-contract-check/bin/pip install -r server/docs/contract/requirements.txt
/tmp/erecipe-contract-check/bin/python server/docs/contract/validate_contract.py
```

Runtime tests cover enrollment, durable retries, conflicts, mocked extraction through publication, authenticated assets, migration, and restart recovery. Schema validation alone does not establish Tailscale deployment, iOS integration, rendering quality, or physical reader behavior; these remain separate checks.
