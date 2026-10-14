# BYOS contract evidence

Checked 2026-10-13 against [official BYOS docs](https://docs.trmnl.com/go/diy/byos), [Terminus device API](https://github.com/usetrmnl/terminus/blob/main/doc/api.adoc), and official firmware commit `d5f98f2b88d09eb0c644a7d19a480ebce6d2b5aa`. The user's installed firmware version is not yet known; the server records FW_VERSION and FW_COMMIT when supplied.

- GET /api/setup receives ID (MAC), returns HTTP 200 with JSON status 200, api_key, friendly_id, absolute image_url, message. Repeated setup for the paired MAC returns the same key. A second MAC is rejected.
- GET /api/display authenticates Access-Token (ACCESS_TOKEN accepted), optionally checks ID, returns HTTP 200 / JSON status 0. filename is an immutable cache identifier; image_url points to the same immutable PNG. refresh_rate is an integer number of requested sleep seconds. reset_firmware/update_firmware are false; special_function is none. Image timeout is 0; maximum_compatibility is false; temperature_profile is default. No OTA target is supplied.
- POST /api/log authenticates the token (ID is optional), accepts a logs array, and returns HTTP 204 with no body. Payloads are limited to 256 KiB. Firmware error/warning messages also appear in the dashboard.
- PNG is sent with image/png, Content-Length, and immutable caching. The X resolution comes from the [official hardware article](https://help.trmnl.com/en/articles/17434702-trmnl-x). Physical rendering remains unverified.

Pinned firmware references:
- [Header construction](https://github.com/usetrmnl/trmnl-firmware/blob/d5f98f2b88d09eb0c644a7d19a480ebce6d2b5aa/lib/trmnl/src/api-client/request_headers.cpp)
- [Display response parser](https://github.com/usetrmnl/trmnl-firmware/blob/d5f98f2b88d09eb0c644a7d19a480ebce6d2b5aa/lib/trmnl/src/parse_response_api_display.cpp)
- [Log success handling](https://github.com/usetrmnl/trmnl-firmware/blob/d5f98f2b88d09eb0c644a7d19a480ebce6d2b5aa/src/api-client/submit_log.cpp)
- [Image transfer and content type](https://github.com/usetrmnl/trmnl-firmware/blob/d5f98f2b88d09eb0c644a7d19a480ebce6d2b5aa/src/bl.cpp)
- [PNG header validation](https://github.com/usetrmnl/trmnl-firmware/blob/d5f98f2b88d09eb0c644a7d19a480ebce6d2b5aa/lib/trmnl/src/png.cpp)
- [Sleep and wake duration](https://github.com/usetrmnl/trmnl-firmware/blob/d5f98f2b88d09eb0c644a7d19a480ebce6d2b5aa/src/sleep_session.cpp)

The header implementation sends Battery-Charging as 0/1 and USB-Connected as true/false; both boolean encodings are handled. PERCENT_CHARGED outside 0–100, NaN, infinity, or missing is unavailable. All other telemetry values are preserved raw, including possible sentinels. The source sets previous wake time using millis() minus startup time and sleeps for the requested interval afterward; this supports measuring, not assuming, a 20-second wall-clock cadence.

Firmware may supply additional gauge fields, Wi-Fi band, panel revision, and commit. The server allowlists these useful fields and never logs arbitrary authentication headers. Current/temperature/capacity sentinels are not converted without a confirmed installed-firmware contract. A wake current sample is not sleep-averaged consumption.

[Official setup/restore steps](https://help.trmnl.com/en/articles/12263392-connect-your-device-to-terminus-byos) and [X touch-bar controls](https://help.trmnl.com/en/articles/17434702-trmnl-x) informed the runbook. No actual user-device access or firmware modification was performed.

## Two-page tap trial

The opt-in dashboard trial uses the same pinned commit above. [Wake-reason mapping](https://github.com/usetrmnl/trmnl-firmware/blob/d5f98f2b88d09eb0c644a7d19a480ebce6d2b5aa/lib/trmnl/src/logging_parsers.cpp) maps GPIO to `button`, RTC inputs to `EXT0`/`EXT1`, timer wake to `timer`, and cold start to `powercycle`. [Startup handling](https://github.com/usetrmnl/trmnl-firmware/blob/d5f98f2b88d09eb0c644a7d19a480ebce6d2b5aa/src/bl.cpp) groups GPIO/EXT0/EXT1 as physical-input wakes. [Touchbar actions](https://github.com/usetrmnl/trmnl-firmware/blob/d5f98f2b88d09eb0c644a7d19a480ebce6d2b5aa/src/touchbar_actions.cpp) display cached left/right images and sleep on success; the middle action continues into the normal network path. The request header carries a wake reason, not a gesture identifier or unique event ID.

Only an active trial advances for those three physical wake sources. Timer/unknown wakes and explicit cached-image reports hold. Each page must first be fetched before advancing away from it; cache reuse needs no subsequent fetch. Three-second debounce suppresses immediate repeated input. Delayed retries and other physical wakes remain ambiguous and require hardware observation. No firmware update, touchbar-mode change, or cloud playlist change is requested. See the [hardware trial steps](../README.md#two-page-tap-trial).
