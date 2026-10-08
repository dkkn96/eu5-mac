# Troubleshooting

## The game stays at 0% while loading a save

Earlier testing on Wine 11.0 showed a high-CPU wait pattern around
`NtWaitForAlertByThreadId` while an existing save remained at 0%. That pattern
was suggestive of a Wine/runtime issue, not proof of save incompatibility. The
controlled follow-up used Wine 11.13 and left game files and saves unchanged.
Check the game and save versions before changing anything, and keep the old
runtime backup so the test can be compared. Save/reload success is still
unverified.

## Steam's Windows window is black or unavailable

The Windows Steam client can start without a useful visible window in a
compatibility wrapper. Sign in through the user's normal Steam flow first, then
use the wrapper's EU5 launch path with `-silent -applaunch 3450310 -vulkan`.
The helper does not read or repair Steam credentials.

## Changing graphics quality or a preset freezes the game

This was observed once in the local reference run after the user changed an
unspecified graphics setting. A bounded recovery backed up the settings and
logs, reset only the persisted `Graphics` object, and restarted Wine 11.13;
the game then reached the game state and the user reported short simulation
advancement without changing graphics settings. The original trigger is still
unknown. Preserve logs and avoid repeated preset changes while diagnosing it.
This toolkit does not edit graphics settings, shader caches, or
`compound_settings.txt`.

## Apply refuses to run

Quit the Sikarugir wrapper and all Windows Steam/EU5 processes. Confirm that
the wrapper is a real Template 1.0.21 app bundle, that the ICD exists and has
bounded MoltenVK 1.4.1 evidence, and that EU5 app ID 3450310 is installed in a
standard Steam library inside the wrapper prefix. Check the pinned archive
size and SHA256; do not substitute an unverified Wine build.

## Restore

Quit the wrapper, then pass the private backup directory printed by `apply` to
`restore`. Restore replaces the current engine and plist and keeps a copy of
the replaced engine inside that backup. Prefix registry snapshots remain
available but are intentionally not restored automatically.
