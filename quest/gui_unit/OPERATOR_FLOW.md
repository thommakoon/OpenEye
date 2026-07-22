# gazeGait Quest session — operator flow (no APK transfer)

Cross-app `launchApp` handoff is **off the critical path** for pilot reliability.
Keep separate APKs; open them manually on the Quest. PC OpenEye GUI only does
TCP, gaze, sync, and Main Study commander.

## Packages

| Role | Package / app |
|------|----------------|
| OpenEye calib | `org.MixedRealityToolkit.MRTK3Sample` (processing_unit) |
| Practice | `com.PracticeMG.MRstressPRACTICE` |
| Main Study | `com.PracticeMG.MRstress` |

## Per participant (interim)

1. **PC:** start `openeye-quest-gui`, Connect Neon, Start TCP.
2. **Quest:** open **OpenEye calib** → calibrate (PC: Start Calibration / Next Step).
3. **Quest:** quit calib → open **Practice** or **Main Study**.
4. **PC:** confirm TCP connected → Gaze Tracking / Visualize as needed.
5. **Main Study only:** use **Main Study commander** (sub / condition / reps / duration → Start condition).
6. **PC hub sync** auto-starts when Neon + Quest TCP are both connected (or use **Start PC hub sync**).
   Writes `sync.json` with `offset_quest_to_pc_ns` and `offset_phone_to_pc_ns` every ~1 s.
   Logs: `sync_quest_echo.jsonl`, `sync_neon_echo.jsonl`.

Do **not** use “Start Practice / Start Main Study / Recalibrate” unless you
enable **Debug: show APK handoff buttons** in the GUI (legacy `launchApp`).

## Wireless ADB (operator opens/quits apps)

Preferred over scrcpy during recording. Config + commands:

See [`scripts/quest_adb/`](../../../scripts/quest_adb/) (`quest_adb.cmd connect`, `switch main`, …).

## Why

Handoff (gaze OFF → `launchApp` → TCP gap → `sessionHello` → resume gaze) is
fragile across process kills and package mismatches. Manual app switch is a few
seconds per session and keeps Fitts / logging work unblocked.

## Later: thin handoff reimplement (not done yet)

When re-enabling transfer, keep this contract only:

1. PC sends `launchApp` + expected package.
2. Target app sends `sessionHello` once on TCP connect.
3. PC resumes gaze only if `hello.package` matches expected.
4. Hard timeout + clear error (no silent fallback guessing).

Do **not** merge `processing_unit` into Practice/Main until this path is stable
and APK switching is still the main daily cost. Prefer one study APK with scenes
over merging calib into both apps.
