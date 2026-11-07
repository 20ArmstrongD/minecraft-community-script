# Recovery without resetting a world

All commands here run as root **inside the Minecraft LXC**, except the explicitly identified Proxmox backup step. Never recreate the guest or reset its world to fix a configuration or tunnel failure.

## Routine failure

1. Read `minecraft info`, `minecraft logs --tail 300 --no-follow`, and `journalctl -u minecraft-server -u minecraft-backup.service`. A malformed configuration does not prevent `minecraft stop` or `minecraft logs`.
2. Check the recorded operation in `/etc/minecraft-server/maintenance`. Correct disk, Java, mod, port or permission failures first. If shutdown timed out, wait for actual Java processes to stop; do not kill a saving world to obtain a backup.
3. If last applied state/config agree and no interrupted file swap remains, run `minecraft recover`, review its confirmation, then explicitly `minecraft start`.
4. If your desired config changes were not committed, run `minecraft recover --discard-pending-config` to explicitly return the management config to the last applied state. This changes no world data. It also clears a routine maintenance inhibit after verifying the game is stopped. Automation requires `--yes`.

A `restore-required` file means older Minecraft binaries were prepared. It is independent of routine maintenance recovery; startup remains blocked until a matching consistent world backup is restored. Do not remove this block manually.

## Recovery itself was interrupted

The tool deliberately refuses to guess if `/etc/minecraft-server/apply-recovery` or `/var/lib/minecraft-server.restore-rollback` remains. Before manual recovery, take a Proxmox backup **from the host** or copy the stopped guest's directories to separate storage, preserving ownership. Keep the original copies and verified archive/checksum.

- `apply-recovery` holds exact pre-change `state.json` and `server.properties`. Keep Minecraft stopped. Inspect the corresponding operation and backup, then restore those files to `/etc/minecraft-server/state.json` (root:minecraft 0640) and `/var/lib/minecraft-server/server.properties` (minecraft:minecraft 0640). Retain the recovery directory under a different name rather than deleting evidence. Run `minecraft recover --discard-pending-config`. If a timer write was interrupted, rerun `minecraft config apply` after correcting permissions/schedule and confirming desired values.
- `minecraft-server.restore-rollback` is the original entire data directory from a restore swap. Keep Minecraft stopped. Do not merge worlds or start either copy. Determine whether the committed state/config match the old data or the requested archive. Move the current directory aside, retain both copies, then restore the appropriate complete data/config/state from the verified pre-restore backup or rollback directory. Preserve the dedicated user's ownership. Use a disposable guest to inspect ambiguous copies. Run `minecraft recover` only once config/state/data consistently describe one version and world.

The full archive format includes `data/`, `config.env` (applied), `desired.env` (possibly pending), `state.json` and `backup.json`. Archives contain no runtime binary or Java tree. Restore never trusts executable paths from an archive. If runtime versions differ, prepare exact saved pins with `minecraft update --no-start`, then restore; an older game release additionally requires the explicit `--allow-downgrade` safeguard.

## Partial installation and code repair

The initial installer records a fingerprint in `/etc/minecraft-server/installing`. To retry, use the original config and explicit `--resume --accept-eula --yes`. It rejects changed choices rather than reinitializing files. Do not use `recover` to bypass incomplete installation.

A completed installation can refresh scripts/units using a reviewed checkout's `bash install.sh --repair`. The game is stopped; versions, worlds, settings and mods are retained. Read logs before explicitly restarting.
