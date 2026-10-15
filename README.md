# eRecipe

A shared recipe library, iOS client, and server that sends rendered recipes to a
TRMNL reader.

- [Run automatically at macOS login on port 10000, with Tailscale Serve](server/docs/MACOS_SERVICE.md)
- [Server setup and reader connection](server/README.md)
- [Application API and phone enrollment](server/docs/SERVER_API.md)
- [iOS client](client_app/README.md)

The installed login service uses `http://YOUR-LAN-IP:10000` for the reader and
`https://YOUR-SERVER.YOUR-TAILNET.ts.net:10000` for the dashboard through Tailscale.
