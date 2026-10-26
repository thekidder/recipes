# Migrated — develop eRecipe in the monorepo

Development moved on **October 26, 2026** to [personal-monorepo/apps/erecipe](https://github.com/thekidder/personal-monorepo/tree/main/apps/erecipe), including both `server/` and `client_app/`.

**Do not implement features, fixes, or deployments from this repository.** Continue in the private monorepo and read its root and app-specific `AGENTS.md`. This repository is retained only for history and recovery.

- Migrated source revision: `8f6b87918b6e826586181e711653f735f73377c0`.
- [Live dashboard and phone enrollment](https://recipes.home.thekidder.com/) (Tailscale required).
- [Deployment and recovery runbook](https://github.com/thekidder/personal-monorepo/blob/main/apps/erecipe/server/docs/MACOS_SERVICE.md).
- The old Tailscale Serve port-10000 application address is retired. Phones must enroll at the new address using their existing user. The TRMNL reader retains its existing LAN endpoint.
- The installed library under the homelab user's `~/Library/Application Support/eRecipe/data/` remains authoritative. Do not deploy or restore this checkout's stale `server/data` or environment over it.

---

## Historical documentation

Instructions below describe the retired checkout and do not override this notice.

No previous agent instructions were present.
