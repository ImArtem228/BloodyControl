# Changelog

## v1.0.1
- Fixed a blocking HID read in DPI status polling: queue draining now uses a 1 ms timeout instead of timeout 0.
- Uses 16-byte input reports, matching the documented vendor-interface report size.
- Switched the DPI stages to the A70 / PMW3327 five-stage profile in the supplied Sensitivity.log: 1000, 1200, 1600, 2000, 6200.
- Updated the computed DPI register pairs and user-facing labels for the A70 sensor profile.

## v1.0
- Moved HID discovery and connection reads to a daemon worker thread.
- Removed automatic device-info and DPI probes during connection; each can be requested from its own tab.
- Moved DPI readback, DPI application, and device-info reads to background workers.
- Added an 8-second connection watchdog and non-blocking status reporting.
- Made HID inventory refresh asynchronous.
- Closing the app no longer runs synchronous HID requests from the UI thread.
- Removed the large DPI compatibility warning card and replaced it with result-oriented messaging.
