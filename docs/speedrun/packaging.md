# Packaging + clean-device install runbook (brief §12)

Deliver an installable desktop app and an installable Android APK, and prove each
installs on a machine/device that has never seen the project.

## Desktop installer

Build the Briefcase-based installer (templates in `qt/installer/`):

```bash
just wheels                     # build the anki/aqt wheels
# then the platform installer (see qt/installer/{mac,linux,windows}-template):
#   macOS:   produces a .dmg / .app
#   Linux:   produces a .tar.zst / AppImage-style bundle
#   Windows: produces a .exe installer
```

TODO: pin the exact installer command per platform and the output artifact path;
attach the built artifact (or a download link) to the hand-in.

**Clean-machine check:** on a machine without the toolchain, install the
artifact, launch, create a profile, open **Speedrun**, study a few questions,
open the dashboard. No Python/Rust/dev tools required.

## Android APK (signed)

```bash
just android-run --rebuild      # dev build onto the emulator/device
# release APK (signed): in Anki-Android/
#   ./gradlew assembleRelease    (uses the release signing config)
```

Signing: use a dedicated release keystore (do **not** commit it). Configure
`Anki-Android` `signingConfigs.release` via env/`keystore.properties` (kept out of
git). Output: `AnkiDroid/build/outputs/apk/release/*.apk`.

TODO: pin the signing setup + attach the signed APK (or install link). Keep the
keystore and passwords out of the repo.

**Clean-device check:** `adb install -r app-release.apk` on a device that never
had the app; open it, sign into AnkiWeb (or point at the sync server), Sync, and
confirm Speedrun study + scores work.

## Hand-in checklist (§12)

- [ ] Desktop installer artifact + clean-machine install confirmed
- [ ] Signed Android APK + clean-device install confirmed
- [ ] 3–5 min demo video (`docs/speedrun/demo-video.md`)
- [ ] Results report with real numbers (`docs/speedrun/results.md`)
- [ ] Brainlift (`docs/speedrun/brainlift.md`)
- [ ] Repo access for graders
