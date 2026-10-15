# Start automatically at macOS login

The login service runs one server process from a local installation under
`~/Library/Application Support/eRecipe`. It starts when you log in, restarts after
an exit, and stops at logout. Installation also starts it immediately.

The installer copies the server, its virtualenv, and `server/.env` into a private
`runtime` directory. On the first install it imports `server/data` into a separate
`data` directory, using SQLite's backup API and an integrity check for the database.
Later installs refresh code and credentials but preserve the installed library.
This keeps the background process and its SQLite database outside iCloud-managed
Documents, which can return file-loading and database I/O errors to login agents.
The original repository data is retained, but is no longer the active library.

| Endpoint | Address |
| --- | --- |
| Reader firmware | `http://YOUR-LAN-IP:10000` (binds this specific LAN IPv4 address) |
| Dashboard and phone API, including on this Mac | `https://YOUR-SERVER.YOUR-TAILNET.ts.net:10000` |
| Private application upstream | `http://127.0.0.1:10001` |

The two HTTP listeners share the same process and database. Tailscale terminates
HTTPS on port 10000 and proxies to the separate application listener on 10001,
which enforces the configured public Host and browser Origin. Proxying to the
reader listener would not provide the phone API and enrollment routes.
Binding the reader specifically to the LAN IPv4 address avoids a macOS port
conflict with Tailscale's listener on the same port. This service does not bind
the reader to localhost; use the HTTPS URL for dashboard controls on this Mac too.

## Install

From the repository root, with Python 3.12+ and Tailscale installed and signed in:

```sh
python3 -m venv server/.venv
server/.venv/bin/python -m pip install -r server/requirements.txt
```

Keep existing credentials in `server/.env`; see [processing setup](PROCESSING.md).
Find the LAN address with `ipconfig getifaddr en0` (or your active interface), and
the Tailscale DNS name with `tailscale status --json`. Reserve the LAN address in
your router so the reader's custom-server URL stays stable. Stop any manually
started eRecipe process first; the server rejects a second owner of its data.

Substitute your addresses, retaining port 10000 on both public origins:

```sh
server/.venv/bin/python server/scripts/install_launch_agent.py \
  --base-url http://YOUR-LAN-IP:10000 \
  --app-origin https://YOUR-SERVER.YOUR-TAILNET.ts.net:10000

tailscale serve --bg --https=10000 http://127.0.0.1:10001
tailscale serve status
```

The installer writes `~/Library/LaunchAgents/com.erecipe.server.plist` with absolute
installation and virtualenv paths, and loads it in your GUI login session. Run it as
your regular user without `sudo`. Add `--dry-run` to inspect the generated plist
without changing anything. Rerunning the installer updates and restarts this job.
It does not configure Serve itself; the explicit Serve command above does that.
Check that port 10000 is unused in `tailscale serve status` before assigning it.

If macOS asks whether Python can access Documents during installation, choose
**Allow** so the installer can read the source checkout. Copying offloaded iCloud
files can take several minutes on the first install. The installed service reads
its local copy and does not depend on Documents access or iCloud downloads.
The copied virtualenv still uses its original base Python installation; keep that
Python installed, or recreate the repo virtualenv with a stable Python and reinstall.

`--bg` makes the Serve configuration persist across Tailscale restarts and reboot;
see the [Tailscale Serve reference](https://tailscale.com/docs/reference/tailscale-cli/serve).
Keep Tailscale enabled at login. This endpoint is available to your tailnet under
its access rules. Dashboard users with access can control the shared library.

Set the TRMNL reader's custom server to the LAN URL including `:10000` (see
[reader setup](../README.md#connect-the-x)). An existing reader configured for
port 8000 must be updated. Phones enrolled with a different application origin
need enrollment using the new Serve URL. Allow incoming TCP 10000 for Python in
the macOS firewall if prompted. Keep the Mac awake and connected to the network;
a login agent does not prevent sleep or run before login.

## Verify and maintain

```sh
launchctl print "gui/$(id -u)/com.erecipe.server"
curl --fail http://YOUR-LAN-IP:10000/health
curl --fail https://YOUR-SERVER.YOUR-TAILNET.ts.net:10000/health
tailscale serve status
tail -n 50 ~/Library/Logs/eRecipe/stderr.log
```

Open the Serve URL to access the dashboard and enrollment. The upstream on 10001
rejects dashboard requests with an ordinary localhost Host header by design.
Phone API routes such as `/api/v1/status` require an enrolled phone's bearer token;
an unauthenticated request should return HTTP 401.
Logs go to `~/Library/Logs/eRecipe/stdout.log` and `stderr.log`; monitor their size
and rotate them while the service is stopped. Application events are in
`~/Library/Application Support/eRecipe/data/events.jsonl`. Back up the complete
installed data directory, using a consistent SQLite backup or stopping the service
first. Do not start the repository's old data copy as if it were the active library.
Credentials are read from the installed `runtime/server/.env`, never copied into
the plist. The explicit application origin overrides `ERECIPE_APP_ORIGIN` from `.env`.

After changing code, dependencies, or `server/.env` in the repo, rerun the installer
with the same origins to refresh the installed runtime and restart. A previous
runtime snapshot is retained as `runtime.previous`; it does not contain the active
data directory. To restart the current installed version without updating it:

```sh
launchctl kickstart -k "gui/$(id -u)/com.erecipe.server"
```

If the checkout moves, recreate its virtualenv before the next install. If the LAN
address or Tailscale DNS name changes, rerun with the new origins and update the
reader/phone configuration. The LAN interface must have the configured address;
if it is unavailable at login, launchd retries until the server can bind it.

To stop and remove automatic startup (preserves recipes and credentials):

```sh
launchctl bootout "gui/$(id -u)/com.erecipe.server"
rm ~/Library/LaunchAgents/com.erecipe.server.plist
tailscale serve --bg --https=10000 off
```

The final command removes only this Serve port. Other Serve services are unaffected.
The local installation and its data are retained under Application Support.
