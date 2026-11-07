# Validation and real-host acceptance

Development platform: macOS, Python 3.14, Bash 3.2. No Proxmox guest, OS, existing Playit agent or tunnel was modified. No game/JRE/Fabric binary was downloaded or executed. Local shell DNS is restricted; official documentation and source were reviewed through browsing, while binary/registry contracts remain subject to integration testing.

Executed locally:

- `bash tools/check.sh --offline`: Python compilation, per-file Bash syntax, installer/host/CLI help and offline unit tests.
- At the current implementation checkpoint: 66 tests discovered, 65 passed, 1 skipped because the sandbox prohibits binding a Unix socket.
- Actual subprocess fixtures exercise premature zero/nonzero Java-like child exits with a mocked listener. Protocol tests validate Minecraft handshake/status framing with mocked sockets.
- Archive tests create real compressed backups and restore real temporary directory trees, check hashes, failure rollback and world/mod/operator preservation. Root ownership, systemd and Proxmox commands are mocked.

ShellCheck, shfmt and systemd-analyze are unavailable locally; their checks are configured in CI, not claimed to have passed. CI also targets Python 3.11/3.13 and permits the real private-console/graceful-stop socket test on Linux. Unit syntax validation substitutes only executable paths in temporary copies; it does not start a service or certify LXC compatibility.

## Before live use

Use a disposable **new** unprivileged Debian 12/13 LXC (amd64 first) with a reviewed checkout, then record:

1. Fresh vanilla install using an explicit official release, EULA and a known numeric seed; ensure properties precede the first launch. Repeat with a text seed in another disposable guest. Verify the generated in-game seed, online-mode and whitelist behavior with licensed clients.
2. Confirm publisher metadata, artifact hashes/architecture, Temurin executable, native libraries and systemd hardening on that host/kernel. Check cgroup memory detection and 6 GiB heap within 8 GiB RAM.
3. Run `minecraft console "list"`, add a whitelist player, stop/start/reboot and confirm the same world/player inventory survives. Confirm no RCON listener and only the intended game port is tunnelled.
4. Fresh Fabric install with an exact compatible loader/installer, verify library checksums and game-JAR path, test a reviewed server-only QoL mod and its dependencies. No mod compatibility is certified by an installer success message.
5. Create a backup while running and while stopped; confirm shutdown/save completion, checksums and resumption state. Restore a selected archive, confirm pre-restore backup, matching pins and no automatic restart. Test failure with insufficient storage, never intentionally damage a production world.
6. Explicitly update a disposable world, verify pre-update backup and prior pins, then test preparing old binaries and restoring the matching old world. Startup must remain blocked between downgrade preparation and restore. Never test raw downgrade against the only world copy.
7. On the existing agent dashboard add a separate **Minecraft Java TCP** tunnel (no modifications to other tunnels). Run the supplied status probe from that agent's actual network namespace to the Minecraft guest. Check the public endpoint separately, then complete login/join/play from outside the LAN. Record these as distinct results.
8. Exercise safe rerun/refusal, same-config resume, management-code repair, daily timer, retention and off-guest backup recovery. Check that no privilege/nesting/AppArmor/firewall weakening was needed.

CPU/RAM defaults are estimates for a light 10-player community, not measured capacity guarantees. Full Proxmox 8/9 × Debian 12/13 × amd64/arm64 coverage remains untested. Older game releases without supported modern metadata, snapshots, Forge/Paper, Bedrock/Geyser, arbitrary extra mod network protocols, remote-agent automation and automatic game updates are out of scope.
