# Personal monorepo and hosting migration plan

Planning date: October 26, 2026.

This document captures the user's decisions and the context needed for an agent
to implement the migration. The destination repository is named
`personal-monorepo`, with applications under `apps/`. This document does not mean
that the migration, DNS changes, or infrastructure deployment have happened.

## Context and objectives

The user develops many personal projects with Codex and other AI tools. Today,
each project generally has its own Git repository and Codex project. Projects
include standalone iOS apps, websites, servers, and combined native clients and
backends. Private services run on the macOS machine `homelab` over Tailscale;
public services run on the server serving `thekidder.com`, which already uses
Caddy. Projects sometimes move from homelab to the public server to reach a
wider audience.

Creating repositories for tiny experiments, maintaining duplicated deployment
configuration, and remembering service ports have become burdensome. The user
wants one home for projects, predictable deployment, and clear instructions so
agents can work from any development host without rediscovering operations.
Projects should remain easy to extract into independent repositories when they
grow, become open source, or need outside collaborators.

An earlier symptom was opening `homelab:10000` and receiving `Client sent an HTTP
request to an HTTPS server.` The browser used HTTP against a Tailscale Serve
HTTPS listener. MagicDNS resolves a hostname; it does not redirect a browser or
upgrade HTTP. The new gateway explicitly supplies the short-name HTTP redirect.

## Agreed decisions

1. Use a private personal monorepo by default, migrating most or all existing
   projects into it. Preserve project boundaries and independent deployment.
2. Continue supporting projects whose code remains in external repositories.
   The monorepo stores their infrastructure registration and routing; the
   external repository owns code and instructions/scripts for deploying it.
3. Use `AGENTS.md` files, with this capitalization, for common and project-level
   instructions. Each commit changes one project, or shared infrastructure;
   never mix these scopes in one commit.
4. Iterate on a local development server by default, even when the development
   machine is not homelab. Projects can override this: eRecipe should deploy
   server iterations to homelab for its physical TRMNL integration. Deploy
   completed server changes to the declared final host and verify them.
5. Share Caddy configuration and a small project directory. Use separate
   subdomains for applications instead of forcing all apps under URL subpaths.
6. Use macOS launchd services managed with `launchctl` on homelab and systemd
   units managed with `systemctl` on the public server. Docker is deferred.
7. Each project's instructions identify its hosting target and give concrete
   deployment, status, logs, verification, and recovery procedures.
8. Use DNS records pointing directly to homelab's Tailscale IP, with Caddy on
   homelab terminating HTTPS for custom domain names using DNS validation.
   Tailscale provides private networking. Tailscale Serve is no longer needed
   for migrated services; retire old routes only after cutover is verified.
9. Cloudflare Tunnel/Access and easier sharing with friends are future work.
10. A one-time TRMNL server URL change is allowed during migration. Preserving
    its existing URL is NOT a requirement. Once migrated, keep its endpoint
    stable during normal iteration; repeatedly reconfiguring the reader is the
    problem to avoid.

Do not treat the monorepo, hosting location, and access policy as the same
decision. An external repository may deploy to homelab; an app in the monorepo
may deploy publicly; an iOS-only app may have no server deployment at all.

## Repository layout and ownership

```text
personal-monorepo/
  AGENTS.md
  README.md
  apps/
    erecipe/
      AGENTS.md
      README.md
      server/
      client_app/
      deploy/
    another-project/
      AGENTS.md
      README.md
      deploy/
  infra/
    AGENTS.md
    projects.yaml
    caddy/
    dashboard/
    hosts/
      homelab/
      thekidder/
  scripts/
  docs/
```

Keep eRecipe's existing `client_app/` directory name during initial import;
renaming it is unnecessary migration work. Keep each project's language,
dependency files, build tools, tests, and application-specific deploy scripts
with that project. Do not impose a common application stack or a large monorepo
build system. Native clients and their backends belong in the same app folder.

Shared infrastructure owns DNS/TLS conventions, gateway configuration, host
inventory, directory generation, and common operational wrappers. Applications
own their processes, dependencies, migrations, data requirements, and health
checks. Infrastructure registration must work identically for internal and
external code locations. Do not assume external repositories live at a specific
developer's absolute checkout path, and do not require Git submodules.

Avoid direct imports from sibling apps. If shared application libraries become
necessary, document and version the dependency or plan how it will be included
when extracting an app. Prefer sharing deployment conventions over creating
tightly coupled application code. Keep secrets, databases, uploaded files,
virtual environments, and build output outside tracked source.

## DNS, networking, and HTTPS

The private request path is:

```text
Device connected to the tailnet
  -> DNS lookup: recipes.home.thekidder.com -> homelab Tailscale IP
  -> HTTPS over Tailscale to homelab:443
  -> Caddy on homelab terminates TLS and selects the hostname
  -> HTTP to the project's loopback listener
```

The public server is not in this request path. Registering a subdomain of
`thekidder.com` does not imply that traffic visits the server hosting the apex
website. DNS chooses the destination independently for each hostname.

Proposed DNS records at the existing authoritative DNS provider:

| Type | Name | Value |
| --- | --- | --- |
| A | `home.thekidder.com` | Actual homelab Tailscale IPv4 address |
| A | `*.home.thekidder.com` | Actual homelab Tailscale IPv4 address |

These must be DNS-only records if the provider offers HTTP proxying. The actual
provider, IP, credentials, and existing records must be discovered; do not copy
placeholder addresses into production. Add IPv6 only when its routing and
listener behavior have been verified. Wildcard DNS is a convenience for future
apps, not authorization to route every hostname to an arbitrary backend.

Public DNS may return a private Tailscale IP. That exposes the DNS mapping, not
network access to the service. Access still requires tailnet connectivity and
the relevant Tailscale access rules. Some local resolvers have DNS rebinding
protections; verify resolution on intended clients and use a narrow DNS solution
if necessary rather than disabling protections globally.

Caddy obtains trusted certificates for the custom domain names through ACME
DNS-01 validation. This requires a compatible DNS-provider module in the Caddy
build and a scoped DNS API credential accessible to the Caddy service. Store
credentials outside Git and persist Caddy's certificate/account storage across
deployments. No public inbound ports or router forwarding are required for DNS
validation. A wildcard certificate for `*.home.thekidder.com` does not cover
`home.thekidder.com`; ensure both the directory hostname and app names are covered.

An alias/CNAME to a `.ts.net` name would not change the browser's requested
hostname or make a Tailscale certificate valid for `home.thekidder.com`. This
design uses custom-domain certificates directly, not a redirect through the
Tailscale hostname.

| Requested address | Intended behavior |
| --- | --- |
| `http://homelab/` | Redirect to `https://home.thekidder.com/` |
| `https://home.thekidder.com/` | Serve the private project directory |
| `https://recipes.home.thekidder.com/` | Proxy to eRecipe's application listener |
| Other registered `https://APP.home.thekidder.com/` | Proxy to that app |
| Unregistered hostname | Reject or return a neutral not-found response |

Caddy can handle the HTTP redirect and serve generated static directory files;
there is no need for another always-running webserver solely for those jobs.
The short entrance is explicitly HTTP. `https://homelab/` is not promised a
publicly trusted short-name certificate.

Discover current listeners before assigning ports 80 and 443. Give one service
clear ownership of each address/port, validate Caddy configuration before reload,
and avoid conflicting wildcard binds or overlapping Tailscale Serve listeners.
Confirm the installed macOS Tailscale variant supports the chosen direct Caddy
listener arrangement from another tailnet device. Use explicit bindings and/or
firewall controls so private application routes are not inadvertently reachable
from the LAN or other interfaces. Device-facing LAN routes, such as TRMNL, are
separate, deliberate exceptions.

The public server retains its own Caddy instance, with app hostnames such as
`recipes.thekidder.com` when appropriate. Migration to that host should reuse
project deployment conventions and change the registered target, hostname,
runtime configuration, and data placement. Public exposure also requires an
appropriate application authentication model; changing DNS alone is not enough.

## Project inventory and directory

Use `infra/projects.yaml` as the authoritative shared registration. Define and
validate a small schema before building generators. At minimum record:

- Stable project ID, display name, description, and active/retired status.
- Source kind: monorepo path or external repository URL.
- Hosting target: `homelab`, `thekidder`, or `none`.
- Canonical application URL and any explicitly distinct device-facing endpoint.
- Internal listener address/port and service name/label.
- Iteration policy: local by default, or named-host integration override.
- Deployment entry point and the location of operational instructions.
- Health-check method and references to data/secret configuration, never secrets.

Make portable registry values authoritative; project instructions should refer
to them instead of maintaining conflicting copies. Record actual host connection
details in host configuration and keep machine-specific paths configurable.
Validate duplicate project IDs, ports on the same interface, hostnames, and
missing source/deployment references. Preserve explicit non-web/client-only
registrations without inventing a web deployment for them.

Generate a simple directory with project links, descriptions, hosting target,
and relevant repository/docs links. Initial availability indicators are optional;
do not build a monitoring platform as a prerequisite. The directory itself is
private. Caddy imports reviewed/generated per-project routes. Keep generator
inputs clear and avoid editing generated output as a second source of truth.

## Agent instructions and iteration policy

Create root and per-project `AGENTS.md` files as migration deliverables. Include
the following requirements in language agents can act on:

### Commit boundaries

- One project per commit. Do not combine changes to two apps in a commit.
- Shared infrastructure commits are separate from application commits, even
  when they support the same feature or migration. Root common instructions and
  the shared registry count as infrastructure.
- Application-specific deployment code and its `AGENTS.md` belong to that app's
  commits. Shared gateway registration belongs to an infrastructure commit.
- Split cross-cutting work into a documented sequence of scoped commits and
  coordinate deployment after the needed commits exist. Do not force unsafe
  partial deployments merely to match commit boundaries.
- Review staged changes explicitly and preserve unrelated user work.

These boundaries make future extraction easier, but do not by themselves make
apps independent; dependency and data boundaries still matter.

### Development and completion

- Read the root instructions, relevant app instructions, and infrastructure
  instructions when touching shared configuration. Explicitly read nested app
  instructions when beginning from the repo root.
- Default to a local server on the current development host for fast iteration.
  Use isolated development data and per-worktree ports where concurrent work
  needs them. Do not assume the checkout is on homelab.
- Honor an app's integration override. For eRecipe, deploy meaningful server
  iterations to homelab and verify there because the physical reader uses it.
  Local unit tests and client-only work can still run locally.
- After iteration and relevant checks, deploy changed server components to the
  registered final target, whether homelab or thekidder. This is the desired
  routine workflow, not an optional offer at the end of each coding task.
- Scope this rule to deployable changes: documentation-only changes and
  client-only apps do not need a server restart. Native client distribution is
  a separate, explicitly documented project workflow.
- Verify the deployed revision, service health, logs, and meaningful user path.
  A local test or successful restart alone is not deployment verification.
- If access, credentials, a failing check, or an approval boundary blocks
  deployment, report the exact blocker and remaining steps; do not claim the
  task is fully deployed. Respect explicit user instructions to defer deployment.
- Use separate Git worktrees for concurrent agent tasks. Separate chats alone
  do not isolate filesystem changes. Instructions and commit boundaries are
  workflow conventions, not a security sandbox between apps.

### Required per-project operations documentation

Document source/build/test commands, final host, local-versus-host iteration
policy, deployment command, connection procedure, service manager and label/unit,
working directory, environment/secret locations, data paths, canonical and device
URLs, health checks, logs, backup/restore, rollback, and removal procedures.
External repos must supply the same information in their own instructions.

## Service and deployment conventions

Use a small shared command interface, for example `deploy PROJECT`,
`status PROJECT`, `logs PROJECT`, and `stop PROJECT`, implemented by thin wrappers
that dispatch to the project's scripts and registered host. These are proposed
interfaces to implement, not commands that already exist.

On macOS, use launchd plist definitions and `launchctl`. Choose and document
LaunchAgent versus LaunchDaemon deliberately: a user LaunchAgent depends on
login and stops at logout; an always-available gateway may have different needs.
Resolve low-port permissions and credential storage explicitly. Do not silently
turn all app processes into root services. On the public server, use systemd
units with explicit runtime users, directories, environment sources, restart
behavior, and journal access.

Deploy snapshots or built artifacts separately from development checkouts.
Record the exact source revision, and keep runtime data outside replaceable
releases. Repeated deployments should preserve data and be safe to rerun. Stage
and validate the next release before replacing the active one; retain a known
working release for rollback. Identify dependencies for external repos by the
actual revision deployed, rather than an unspecified moving branch.

Keep per-app dependencies, credentials, and data permissions separate. Assign
loopback ports centrally where needed, but hide them behind named URLs for
people. Provide log rotation/retention, backup procedures, and a basic health
check. Apply schema migrations deliberately; reverting code does not necessarily
revert a database migration.

## eRecipe: current facts and migration requirements

These facts were read from this checkout on the planning date. They are not a
live audit of homelab. The local `tailscale serve status` attempt earlier in the
conversation failed with `Failed to load preferences`; discover actual state on
the deployment host before changing it.

Current references, relative to this repository root:

- `server/docs/MACOS_SERVICE.md`: current installation and operations runbook.
- `server/scripts/install_launch_agent.py`: runtime installer and origin checks.
- `server/api/http.py`: application Host, Origin, and loopback ingress checks.
- `server/app.py`: reader-facing origin configuration.
- `client_app/README.md`: native client, enrollment, and distribution workflow.

Current documented deployment:

| Component | Current configuration |
| --- | --- |
| Reader listener | HTTP on the specific LAN IPv4 address, port 10000 |
| Private application upstream | `http://127.0.0.1:10001` |
| Tailnet application URL | `https://SERVER.TAILNET.ts.net:10000` through Serve |
| Runtime and data parent | `~/Library/Application Support/eRecipe` |
| Installed code | `runtime/` under that parent |
| Active persisted state | `data/` under that parent |
| Previous runtime snapshot | `runtime.previous/` under that parent |
| Service | `com.erecipe.server` in the user's GUI launchd domain |
| Logs | `~/Library/Logs/eRecipe/stdout.log` and `stderr.log` |

The installed library is authoritative; the repository's `server/data` may be
stale. Do not overwrite installed data during source migration or accidentally
run two processes against the active database. Back up SQLite consistently using
its backup API or a stopped-service procedure, along with required assets.

Required work:

1. Import the complete project under `apps/erecipe/`, preserving server/client
   relationships. Audit repository-root assumptions, scripts, generated Xcode
   files, ignored signing configuration, and virtualenv paths after relocation.
   Recreate environments where relocation makes them invalid.
2. Adapt the installer: it currently requires both public origins to use port
   10000 and serializes the app origin with that port. Support the new HTTPS
   origin on standard port 443 without weakening hostname/origin validation.
   If changing the reader endpoint, remove its inappropriate hardcoded port or
   IP restrictions as required by the selected design.
3. Route `recipes.home.thekidder.com` to the dedicated application listener,
   not the reader listener. Preserve the browser Host and Origin semantics and
   the application's loopback-only ingress boundary. Update Serve-specific
   wording where Caddy is now the ingress, without removing request protections.
4. Review proxy header handling. Tailscale identity headers are currently optional
   rate-limit metadata, not administrator authentication. Do not trust arbitrary
   client-supplied identity headers when replacing Serve; remove or deliberately
   supply trusted metadata at the gateway, and verify rate-limit behavior.
5. Set the canonical application origin to the new URL and update enrollment
   links, documentation, and native client configuration. Plan for re-enrollment
   or an explicit supported transition: the current API validates one configured
   origin, so two working origins must not be assumed during rollout. Native
   API calls must not depend on a browser redirect following a hostname change.
6. Choose and document the final TRMNL endpoint. A one-time device URL update is
   authorized as part of migration. The reader must resolve and reach that URL
   on its own network; do not assume it runs Tailscale, understands MagicDNS,
   supports a particular TLS setup, or can reach a Tailscale-only IP.
7. Prefer a stable, explicitly configured LAN/device endpoint compatible with
   the reader. This can be a dedicated Caddy LAN route with suitable local DNS,
   or a registered LAN listener if that is simpler. If Caddy fronts it, expose
   only the intended reader functionality; inspect existing routes before
   proxying a whole listener. Do not make the administrative application
   reachable on the LAN merely to serve the reader.
8. Update the reader base URL used in generated image links as well as the URL
   entered on the device. Verify DNS, polling, image fetches, and physical
   display on the actual TRMNL. Keep its new endpoint stable afterward.
9. Deploy server iterations to homelab per the app's `AGENTS.md`. At final
   cutover, verify browser controls, enrollment, native-client requests, jobs,
   persisted recipes, and physical reader delivery.

The current dashboard trusts anyone allowed to reach its private service; it
does not have a separate administrator login. Preserve that private boundary.
If eRecipe is later made public, design its authentication/authorization before
exposure rather than treating that move as a hostname-only change.

## Migration sequence

### 1. Discover and record the existing state

Inventory repositories and dirty worktrees; identify which to import and which
to register externally. Record current host connections, processes, ports,
service definitions, DNS, Caddy/Serve routes, runtime paths, data, and dependencies.
Locate the DNS provider and determine how Caddy will perform DNS validation.
Inspect existing instructions before changing each repository. Record unknowns
and get missing operational inputs when necessary; do not invent them.

### 2. Establish the monorepo foundation

Create `personal-monorepo` at a confirmed filesystem location and remote. Add the
layout, root/infrastructure instructions, schema, and initial operational docs.
Keep the repository private. Choose a history-preserving import method, record
source remotes and revisions, and perform history transformations only in
disposable clones. Do not rewrite or delete original repositories. Scan tracked
content/history for secrets or private data before importing or publishing.

Import each app's history under its own directory where practical, in separate
project-scoped changes. Record any deliberate history limitations. Commit
shared registration separately. Preserve originals as rollback/reference
sources until each migration is accepted; avoid maintaining two competing
authoritative development locations afterward.

### 3. Build shared infrastructure with one reference app

Implement registry validation, gateway imports, the static directory, host
service definitions, and thin operational wrappers. Add an eRecipe registration
and its project instructions. Add external-repository support without requiring
all imports to finish first. Validate generated routes and service definitions
before installing them. Test the DNS-provider Caddy build and persistent TLS
storage, and record how it is updated.

### 4. Bring up the private gateway

Create DNS records after recording existing state. Deploy Caddy on homelab,
issue certificates, and verify both the directory hostname and app subdomain
from another tailnet device. Test `http://homelab/` redirection, denied access
where expected, unknown-host rejection, and listener conflicts. Leave old
service endpoints available until their replacements are ready where feasible.

### 5. Migrate eRecipe end to end

Back up the active installation. Make the application/installer changes in app
commits and gateway changes in infrastructure commits. Select the device-facing
endpoint, perform the one-time TRMNL update if needed, and coordinate the
application-origin/native-client transition. Deploy the new revision, run the
checks above, and record results and rollback steps. Retire only the obsolete
eRecipe Serve configuration once the replacement is verified; preserve unrelated
Serve services.

### 6. Migrate the remaining projects incrementally

For each app: import or register externally, document operations and iteration
policy, allocate its route/service, migrate data if needed, deploy, verify, and
retire old launchers/configuration. Client-only apps need source/build workflow
migration rather than a fabricated server deployment. Apply the same contract
to the public host while preserving its existing unrelated Caddy sites.

### 7. Verify extraction and hand off operations

Use a disposable clone to demonstrate that one app can be extracted into a
standalone repository with relevant history and documented dependencies. Its
monorepo registration should switch to an external source without changing the
hosting contract. Update Codex project paths/workflows and other tool references
to the new checkout. Publish a concise runbook with examples for new apps,
deployment, logs, rollback, retirement, and external registrations.

## Rollback and acceptance

Before each cutover, record the previous code revision, service definition,
gateway configuration, canonical origins, device settings, DNS records, and
consistent data backup. Roll back only affected routes and services. If the
reader URL changes, include restoring its configuration in the rollback plan.
Do not restore an older database over new writes without an explicit recovery
decision. A code snapshot alone is not a data backup.

The migration is complete when:

- Imported projects and external registrations have clear authoritative sources.
- Root and project `AGENTS.md` files contain the agreed commit and deployment rules.
- The private directory is reachable through the short HTTP entrance and its
  trusted HTTPS hostname; registered apps have working separate subdomains.
- Private services remain private, and intentional reader LAN access works.
- Service startup/restart behavior is tested for the chosen launchd/systemd scope.
- Agents can deploy from a different development machine and inspect remote logs.
- eRecipe's real library, iOS enrollment/API, and TRMNL delivery work after migration.
- Per-app backups, rollback, source revisions, and persistent state are documented.
- Old ports/routes are removed only after successful replacement, with no damage
  to unrelated apps; remaining exceptions are listed rather than forgotten.
- A small project can be added without creating a new repo or bespoke host setup.

## Deferred work and inputs to resolve

Deferred: Docker, Kubernetes or other orchestration, a large shared build system,
Cloudflare Tunnel/Access, browser-based friend authentication, and a rich control
panel. The user is interested in sharing without requiring friends to install a
VPN, but that is not part of this initial migration. Tailscale user limits and
friend onboarding friction motivated that future exploration; do not assume
current pricing or limits without checking at implementation time.

Inputs still to discover: destination checkout/remote, complete project list,
host connection details and service users, DNS provider/module/token scope,
actual Tailscale address/access rules, current port ownership, final TRMNL endpoint
and firmware/network capabilities, and any app-specific transition constraints.
The architecture is decided; these are implementation facts, not reasons to
reopen the entire design.

## Reference material

- [Caddy automatic HTTPS and DNS validation](https://caddyserver.com/docs/automatic-https)
- [Caddy TLS configuration and DNS-provider modules](https://caddyserver.com/docs/caddyfile/directives/tls)
- [Caddy routing patterns and subpath limitations](https://caddyserver.com/docs/caddyfile/patterns)
- [Tailscale DNS](https://tailscale.com/docs/reference/dns-in-tailscale)
- [Tailscale HTTPS certificate names](https://tailscale.com/docs/how-to/set-up-https-certificates)
- [Extracting a folder into a repository](https://docs.github.com/en/get-started/using-git/splitting-a-subfolder-out-into-a-new-repository)
- [Codex repository instructions](https://learn.chatgpt.com/docs/customization/overview)
- [Codex worktrees](https://learn.chatgpt.com/docs/environments/git-worktrees)

Consult current official documentation when implementing provider-specific or
version-dependent details. Examples in this plan describe the intended design;
they are not a claim that host configuration has been inspected or deployed.
