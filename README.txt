Bloody Control A70 · v1.0.1
========================

A Windows desktop utility for Bloody A-series mice, tailored for the A70 HID layout.

FEATURES
--------
- Non-blocking USB/HID discovery: searching and device replies run in background threads.
- Connection timeout feedback: a slow driver should not freeze the application window.
- Backlight control with four levels (off, low, medium, high), including read-back checks.
- Software light patterns built from those four available hardware levels.
- DPI page with 1000 / 1200 / 1600 / 2000 / 6200 presets, a current DPI-slot display, and response checking.
- Profiles with DPI, backlight level, effect and speed; import/export and profile rotation.
- Auto-off timer, HID interface list, device information, diagnostic report and log export.
- Settings are stored separately in %USERPROFILE%\BloodyControlA70\profiles.json.

RUN
---
1. Extract the ZIP to a normal folder.
2. Run run.bat.
3. Click “Найти A70”.
4. Use “DPI / Сенсор” to read the current slot and apply a DPI preset.

REQUIREMENTS
------------
- Windows 10/11
- Python 3.10+ with Tkinter
- Python package hidapi (run.bat offers to install it if missing)

DPI TABLE
---------
The five displayed DPI values match the supplied Workshop Sensitivity.log / SENSOR_3327_1
profile for the Bloody A70 PMW3327 sensor. The HID protocol exposes a
current slot index, and the app verifies whether the mouse reports the selected slot after
applying a preset. If Workshop's DPI table was customized, update the map in the source to
match your saved configuration. A response that does not confirm the selected slot is shown
as unconfirmed instead of being presented as success.

TROUBLESHOOTING
---------------
- Close Bloody Workshop before connecting; only one program should communicate with the
  target interface at a time.
- If connecting exceeds eight seconds, the main window remains usable and reports a timeout.
  Retry after closing other mouse utilities or unplugging/reconnecting the mouse.
- DPI status polling uses bounded reads. The previous zero-timeout queue-drain read was removed
  because it could wait indefinitely on a HID stack with no queued input report.

The app does not write firmware or flash/configuration memory. DPI and device-info requests
are live HID requests only.
