# Stage 1 physical experiment

Record this before each run: run ID; date; operator; server commit/source snapshot and requirements.txt; X firmware and commit; one/two battery configuration if known; initial charge; Wi-Fi band/SSID label; RSSI; room/location; requested interval; one-bit PNG landscape; mode; dock disconnected time. Keep personal SSID details out of shared results.

1. Start the server and connect the X using the README. Keep the computer awake and the IP stable. Confirm /health from another LAN client before changing the X configuration.
2. Publish A, B, A again (cached), and a fresh A version. Verify labels and full recipe text. Confirm physical readability separately from image requests.
3. Restart the server using the same data directory. Check credentials, image version, and selection persist. Observe any screen overwritten by device boot/setup/error behavior.
4. At 20 seconds requested sleep, collect at least 10 check-ins. Compare actual intervals, wake time, RSSI, image-transfer events, and firmware logs. Diagnose discrepancies before trying 10 or 30 seconds.
5. Start a new run. Execute 30 fresh-version trials at varied offsets using the trial script; film publication and screen together. Record fully readable time. Report n, median, nearest-rank p95, max, all >30s misses, and all >60s failures. Repeat cached reselections separately. Unobserved trials remain pending.
6. Start another run in soak mode, publish one image, then leave unchanged for two hours. Check stable filename and absence of repeated image requests; inspect video/device for unwanted redraws. Request logs alone cannot prove no redraw.
7. Fully charge the X, disconnect the dock's power, start a new soak run at the same polling policy, and click Mark unplugged. Run for 72 hours with a few fresh recipe publications daily. Do not publish diagnostics during this workload. Log daily operational checks, all charge samples and any server/network gaps. The elapsed timer is time since an operator mark, not proof of continuous operation. A projection is not a battery pass.
8. In separate labeled runs, interrupt the server, restore it, and measure recovery; then interrupt Wi-Fi and restore it. Record retries, screen replacement/error screens, recovery time, and manual actions. Test low battery only when practical and record untested behavior as pending.
9. Read A/B and LONG at real cooking distance. Photograph the display and note clipped text, readability, refresh artifacts, and need for pagination. No stage 2 extraction or iOS code is included.

Use stress mode only in a separate run to measure redraw cost; it changes the image after every check-in and is not representative of recipe use. Changing mode or interval requires a new unplugged mark and preferably a new run.

Export a report:
```sh
server/.venv/bin/python -m server.report server/data/events.jsonl > server/data/report.json
```
Keep the raw log, report, configuration record, and video timing notes together. The report explicitly retains pending trials and lists misses. Compare observations against:
- <=30 seconds baseline publication-to-readable target, leaving extraction time;
- >60 seconds is a normal-operation requirement failure;
- >=72 hours actually unplugged and operational;
- stable unchanged image and no poll-only redraw;
- current image survives server restart;
- recovery resumes and screen replacement behavior is documented.

Do not mark the full workflow proven if only server tests pass. If 30 seconds is missed but 60 seconds met, stage 1 is conditional on remaining extraction time. Record outages separately without deleting them from raw data.

Restore the prior server configuration through the X pairing portal. For hosted TRMNL, use Advanced → Soft Reset and leave API Server URL blank during setup. Stop this server with Ctrl-C; preserve data/state.json to retain pairing and selections for a future test.
