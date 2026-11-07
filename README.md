# Minecraft Community Script

A small Minecraft **Java Edition** installer and management CLI for a dedicated, unprivileged Proxmox LXC. Supports vanilla and Fabric, explicit version pins, a pre-generation world seed, a non-root systemd service, consistent stopped backups, and a separate tunnel on an **existing** Playit agent. No Docker, extra Playit registration, privileged LXC, nesting or host Docker socket is needed.

This is a standalone repository, not an official Mojang, Fabric, Playit or Proxmox Community Scripts release. Local development validation is described in [VALIDATION.md](docs/VALIDATION.md); a real Proxmox/game/client acceptance test is still required.

## Defaults and assumptions

Start with **4 vCPUs, 8192 MiB RAM, 1024 MiB swap, 40 GiB disk**, Debian 12 or 13 with systemd, and Java heap **2048–6144 MiB**. This assumes at most 10 concurrent players, vanilla or light quality-of-life mods, view distance 10, simulation distance 6 and SSD storage. Busy exploration, distant players, farms and large modpacks can exceed these defaults; CPU single-thread performance matters. Backups share the guest disk, so large worlds need more storage and off-guest copies.

Both amd64 and arm64 Linux are supported in the code; neither has been live-tested here. The host wrapper targets official amd64 Proxmox VE 8/9 with amd64 templates; arm64 is an existing-guest installer target only. Give Java at least 1 GiB additional RAM for native allocations/OS; the defaults leave 2 GiB. Heap checks use the effective LXC cgroup memory limit, not just host RAM. Swap is not a substitute for enough RAM.

The exact Minecraft release is selected during setup and saved. Mojang's version metadata determines Java 17, 21 or 25; unknown requirements fail with an error rather than guessing. Current releases can require Java 25. Verified Temurin JRE archives are installed privately under `/opt/minecraft-server/java`, so Debian 12 need not supply Java 21/25 itself. Java patch build, URL and checksum are recorded. Fabric requires explicit loader **and installer** versions.

## Files and conventions

```text
install.sh                         existing-guest setup
ct/minecraft.sh                    optional NEW-LXC wizard
install/minecraft-install.sh        guest entrypoint used by the host wrapper
scripts/                           configuration, artifacts, management, console, probe
systemd/                           game service and backup service/timer
config/example.env                 commented literal configuration
README.md, docs/                   operations, research, validation
tests/                            offline tests (no accounts/live guests)
tools/check.sh, .github/workflows/  local checks and CI
```

The accessible local Valheim and Astroneer repositories were reviewed. This project follows their lifecycle CLI, root-owned data configuration, maintenance inhibition, stopped backups and state-preserving operations. It uses Python's standard library to parse data and archives safely. The optional host wrapper follows Astroneer's reviewed-checkout pattern and Valheim's resource/wizard conventions; it does **not** source a mutable remote Community Scripts engine. Neither reference repository was modified.

## Install inside an existing Debian LXC

Use a dedicated, unprivileged guest. Do not run the guest installer on the Proxmox node. Copy a trusted reviewed checkout into the LXC; there is no unverified `curl | bash` bootstrap or assumed GitHub release.

Inside the LXC as root, install the small bootstrap dependency from Debian's signed repositories, then run the wizard:

```bash
apt-get update
apt-get install -y --no-install-recommends python3 ca-certificates
cd /root/minecraft-community-script
bash install.sh
```

The wizard asks for exact Minecraft/Fabric versions, world directory, optional seed, heap, port, player limit, difficulty, mode, whitelist and distances. Read the [Minecraft EULA](https://www.minecraft.net/eula) and type `ACCEPT` when prompted. Installation writes `level-name` and `level-seed` **before the first game start**.

Unattended equivalent:

```bash
cp config/example.env /root/minecraft-setup.env
chmod 600 /root/minecraft-setup.env
# Edit this file as data: choose exact versions, seed and settings.
bash install.sh --config /root/minecraft-setup.env --accept-eula --yes
```

A config value `eula=true` alone does not grant setup consent; `--accept-eula` is mandatory for config-file installation. Use `--no-start` to install without generating the first world yet. Rerunning setup refuses an existing installation. Partial setup requires explicit `--resume` with exactly the original configuration and EULA acceptance. Completed installations use `minecraft update` or the code-only `--repair` path described below.

## Create a new LXC from the Proxmox host

This optional command runs **on the Proxmox node** as root, from the same reviewed checkout. First use normal `pveam` commands to obtain an official Debian 12/13 template. Replace the placeholder with its actual storage volume ID:

```bash
bash ct/minecraft.sh --template TEMPLATE_STORAGE:vztmpl/DEBIAN_TEMPLATE_amd64.tar.zst
```

It asks for a new guest ID, hostname, storage, bridge, CPU/RAM/swap/disk, DHCP or static IPv4, gateway, VLAN and DNS, then the game settings/EULA. It requires `CREATE` before creating a guest. No existing ID is overwritten, modified or destroyed. A failed setup retains its new guest for inspection; it never deletes a partial guest.

Unattended example (replace values for your environment):

```bash
bash ct/minecraft.sh --template "$TEMPLATE_VOLUME" --ctid "$NEW_UNUSED_CTID" \
  --config /root/minecraft-setup.env --accept-eula --yes \
  --storage "$CT_STORAGE" --bridge "$CT_BRIDGE" \
  --cores 4 --ram 8192 --swap 1024 --disk 40
```

Use `--ipv4 ADDRESS/PREFIX --gateway GATEWAY`, optional `--vlan TAG`, `--dns DNS_IPV4`, `--hostname NAME` and `--no-start`. Default networking is DHCP. No firewall rules are changed; maintain narrowly scoped access from the shared agent to the game TCP port. Static guest DHCP reservation/addressing prevents tunnel destination drift.

## Configuration and first-world seed

Persistent management config: **`/etc/minecraft-server/config.env`**, root:minecraft `0640`, directory `0750`. [The example](config/example.env) documents every key. It is literal `key=value` data: no shell quotes, `export`, substitutions or inline comments. Full-line `#` comments are accepted. Do not `source` it. Spaces, punctuation, backslashes, `=` and Unicode in seeds/MOTDs are preserved; unknown/duplicate/missing keys fail validation.

```text
world_name=world
seed=A quiet valley = friends # 2026
```

A blank `seed=` asks Minecraft for a random seed. Numeric and text seeds are passed unchanged; Minecraft itself interprets them. Do not add shell quotes unless quotes are intended to be part of the seed.

**Set the seed before the first start.** Once the world exists, changing `seed` does not change its stored seed or regenerate its terrain; the CLI warns. The installer, update, restart and configuration commands never delete/reset worlds. Changing `world_name` after installation is refused to prevent accidental generation of a different world. This release deliberately has no new-world/reset command.

After editing management configuration:

```bash
minecraft config check
minecraft config apply
```

Apply stops the game if needed, makes a non-pruning backup, rewrites only the managed properties, then resumes only a previously running game. Managed properties: port, world name/seed, player cap, difficulty, mode, whitelist/enforcement, MOTD and distances. Online authentication and status are enabled; RCON/query are disabled; the listening address remains unbound to an advertised public IP. Edit **unrelated** settings directly in `/var/lib/minecraft-server/server.properties`; they survive apply/update. Duplicate/escaped managed keys and their continuation bodies are replaced safely. Minecraft release/loader changes require `minecraft update`, not just config apply.

Whitelist is enabled by default. After startup:

```bash
minecraft console "whitelist add YOUR_JAVA_PLAYER_NAME"
minecraft console "op YOUR_ADMIN_PLAYER_NAME"
```

## Management inside the LXC

All commands below run as root **inside the guest**, from any working directory. From the Proxmox host use `pct enter YOUR_CT_ID` first, or `pct exec YOUR_CT_ID -- minecraft info`.

| Command | Purpose |
| --- | --- |
| `minecraft start`, `stop`, `restart` | systemd lifecycle; graceful Minecraft `stop` with up to 180 seconds; never force-kills a saving world |
| `minecraft status` | service health, versions, local address/port, configured public endpoint and an independent Minecraft status probe; inactive service exits 3 |
| `minecraft info` | configuration/runtime/resource and maintenance diagnostics |
| `minecraft logs` | follow game/Java journal; Ctrl+C closes the viewer only |
| `minecraft logs --tail 300 --no-follow` | bounded historical journal output |
| `minecraft console "COMMAND"` | send one command through the private local Unix socket |
| `minecraft console` | interactive command entry; responses appear in `minecraft logs`; Ctrl+D exits client |
| `minecraft backup` | stop, archive, verify checksum/content, retain, resume if previously running |
| `minecraft restore ARCHIVE --yes` | verified stopped restore, pre-restore backup, leave stopped; `--yes` explicitly approves replacement |
| `minecraft config check/show/apply` | validate/view/explicitly apply managed settings |
| `minecraft update --help` | explicit version/loader changes only |
| `minecraft network configure` | record existing agent location, guest destination and new public endpoint |
| `minecraft network test [--public]` | Minecraft status probe from this guest; does not certify an external join |
| `minecraft version`, `minecraft --help` | manager version and command help |

The service account is `minecraft`, has no login shell and no capabilities. systemd restricts writes to game data and private runtime paths. A process running is not proof of a healthy world: inspect the game log and status response. No automatic game restart loop is configured. Unexpected Java exit, including status zero, fails the supervisor.

Console access uses `/run/minecraft-server/console.sock`, mode `0600`, inside a private runtime directory. RCON is not enabled, so there is no RCON password or port. Use `minecraft stop`, not a console `stop`; `save-off` is rejected to protect backup consistency. Mods and other code running as the game user can still access that user's data/console; install only reviewed code.

## Existing Playit agent

Run `minecraft network configure` in the guest. It asks where your existing agent runs and how it reaches the LXC (same LAN, routed network or VPN), the Minecraft guest's assigned IPv4, and the actual public `hostname:port` from your **new, separate Minecraft Java TCP tunnel**. It installs no agent, reads no credentials and makes no provider changes.

In the Playit dashboard select your **existing agent**, add a **Minecraft Java** TCP tunnel, and set its local destination to the LXC's reachable address and game port (normally `25565`). Leave **Proxy Protocol off** for vanilla/Fabric. Do not change the Astroneer tunnel. Custom TCP can be used where your plan supports it and a mod requires it; provider limits/protocol restrictions vary.

| Address/port | Meaning |
| --- | --- |
| LXC IPv4 + Minecraft TCP port | where Minecraft listens |
| Playit local destination | the same reachable LXC IPv4 and listening port, viewed from the agent host |
| Playit public hostname/IP + allocated TCP port | what players enter; the external TCP port **may differ** from the listening port |

Minecraft TCP forwarding does not inherit Astroneer's matching-public-port restriction. A remote agent's `127.0.0.1` points to the agent machine, **not** the Minecraft LXC. The wizard rejects loopback/foreign destinations; ensure the agent has a real LAN/VPN route. No router forwarding is needed for this tunnel. Tunnel **only the game TCP port**: never SSH, the Unix console, or management interfaces.

Automation:

```bash
minecraft network configure --agent-location 'Shared agent on another LXC, same LAN' \
  --destination-ip "$MINECRAFT_GUEST_IPV4" \
  --public-endpoint "$PLAYIT_HOSTNAME:$PLAYIT_PUBLIC_TCP_PORT" --yes
```

Endpoint metadata changes avoid restarting the game. To verify the **agent-to-guest** leg, copy the standalone `scripts/probe.py` to the agent host and run there:

```bash
python3 probe.py --host "$MINECRAFT_GUEST_IPV4" --port 25565
```

A PASS means Minecraft's Java status protocol responded **from that host**. If the agent runs inside another Docker/network namespace, run the probe from that same namespace. In the game guest, `minecraft network test` checks local Minecraft and `--public` checks the saved public endpoint from the guest. Neither proves a separate player's authentication/join. Finally join from a compatible licensed Java client **outside your LAN**, using the new public endpoint, and verify sustained gameplay. Until these tests are performed, agent/tunnel/client reachability is reported as unverified. See [official Playit instructions](https://playit.gg/support/how-to-setup-a-mc-server/).

## Fabric mods and multiplayer sleep

Select Fabric initially, or explicitly migrate using `minecraft update --loader fabric` with the current Minecraft version and exact Fabric loader/installer versions. Backups happen before activation; saves and existing mods are kept. The installer downloads the official pinned installer, verifies its published checksum, runs it as the game user without starting Minecraft, and independently verifies fetched libraries against publisher Maven checksums. No optional mod is silently downloaded.

Stop and back up before installing trusted, **server-compatible Fabric** JARs into `/var/lib/minecraft-server/mods/`; preserve ownership `minecraft:minecraft`, files `0640`. Review each mod's exact Minecraft/loader requirements, license, required dependencies (often Fabric API), and whether clients also need it. Configuration is usually in `/var/lib/minecraft-server/config/`. Paper/Bukkit plugins and Forge mods are not interchangeable with Fabric mods.

Multiplayer sleep does not necessarily require a mod. For the example **Minecraft 1.21.1** pin, this vanilla command lets one of up to 10 players skip the night:

```bash
minecraft console "gamerule playersSleepingPercentage 1"
```

Newer releases renamed gamerules; use the selected version's in-game command completion/release notes rather than assuming the old spelling. Gamerules persist in the world and are not overwritten by config apply. Use Fabric when you want additional mod-specific behavior; test the chosen mods in a disposable world first.

## Updates, backups and restore

No unattended game upgrades. To update, select exact versions and explicitly approve:

```bash
minecraft update --minecraft-version TARGET_EXACT_VERSION
# Fabric requires compatible pinned versions too:
minecraft update --minecraft-version TARGET_EXACT_VERSION --loader fabric \
  --fabric-loader-version EXACT_LOADER --fabric-installer-version EXACT_INSTALLER
```

Add `--yes` for automation and `--no-start` to leave the game stopped. A runtime is staged and hashes verified before activation. Maintenance then stops the service, verifies it is stopped, makes a backup, records the prior pins, and activates the selected runtime. Prior releases are kept. Fabric remapping caches are renamed/preserved across updates rather than deleting mods/worlds. Check mod compatibility yourself; the installer cannot certify arbitrary combinations.

Backups: `/var/backups/minecraft-server/minecraft-UTC_TIMESTAMP.tar.gz` plus `.sha256`, root-only. They include **all game data** (worlds/dimensions, player data, server.properties, ops, whitelist, mods and their configs), the last applied management config, pending desired config separately, runtime provenance and prior version pins. They exclude downloaded binaries/JREs; keep their recorded releases or be able to download the pinned versions again. Copy archives/checksums off the guest. A stopped backup cannot save players' unsaved progress if graceful shutdown already failed.

Scheduled backups default to 05:00 guest local time plus up to 5 minutes jitter. Change `backup_enabled`, `backup_calendar` and `backup_retention`, then `minecraft config apply`. Successful manual/scheduled backups prune to retention (default 14); pre-update/config/restore backups do not prune during that operation, but later normal retention applies to them too. Missed runs are not replayed at boot. Monitor `systemctl list-timers minecraft-backup.timer` and `journalctl -u minecraft-backup.service`.

```bash
minecraft restore /var/backups/minecraft-server/SELECTED_BACKUP.tar.gz
```

Restore requires explicit confirmation (or `--yes`), checksum and archive validation, sufficient staging space, and **matching installed version/loader pins**. It makes a pre-restore backup, replaces the data with the selected consistent snapshot and leaves Minecraft stopped. It never executes runtime paths supplied by an archive; locally verified matching runtime paths are used. Check permissions/configuration, then `minecraft start`.

**Older server binaries alone cannot safely downgrade a newer world.** To recover a pre-upgrade world, prepare its exact runtime using `update --no-start --allow-downgrade --yes` plus explicit saved version/loader flags, then restore that matching archive. Downgrade preparation sets a persistent startup block until the matching restore succeeds. Do not delete the block to open a newer world with older binaries.

## Troubleshooting, repair and recovery

- Config errors: `minecraft config check`; correct literal values, then apply. Only explicitly managed keys overwrite server.properties; version/world identity changes have additional guards.
- Service active but not ready: `minecraft info`, `minecraft logs --tail 300 --no-follow`, `journalctl -u minecraft-server`. Wait for world generation. Check Java/mod exceptions, memory headroom, free disk and the game port. `ss -ltnp` shows listeners; the status probe checks Minecraft protocol rather than merely accepting TCP.
- Local game healthy but external join fails: run the probe from the **shared agent host**, check its route/firewalls, separate TCP tunnel allocation/destination and dashboard status, then test from outside. No agent state is guessed from the game guest.
- Failed backup/update/restore: a persistent `/etc/minecraft-server/maintenance` marker blocks starts, including after reboot. Read the error and backups first. No forced kill or automatic retry occurs. After correcting the cause, `minecraft recover` verifies the service is stopped and config matches the last applied state, clears only that inhibit, and leaves the game stopped. `--yes` is available for explicit automation.
- Failed config apply with edited settings still pending: the previous state/properties are restored. After inspecting the failure, `minecraft recover --discard-pending-config` explicitly restores desired settings from the last committed state and clears maintenance inhibition; it never changes worlds. Or keep your intended settings and follow [RECOVERY.md](docs/RECOVERY.md).
- An `apply-recovery` directory or `minecraft-server.restore-rollback` directory means recovery itself was interrupted. Do not delete it or bypass inhibition: see [RECOVERY.md](docs/RECOVERY.md).
- Partial installation: rerun `bash install.sh --config ORIGINAL_CONFIG --accept-eula --yes --resume`. It validates the original choices and preserves any existing world. A setup that failed before writing its marker can be safely retried without `--resume`.

To update **management code**, copy a new reviewed checkout into the guest and run `bash install.sh --repair`. It stops the game, replaces only managed scripts/units and reapplies the existing backup schedule. Versions, seed, settings, mods and worlds remain. It leaves the game stopped for review. Do not rerun fresh setup or host creation against this guest.

## Validation and licensing

Run `bash tools/check.sh --offline` for Python compilation, offline tests, per-file Bash syntax and CLI help. Full `bash tools/check.sh` also requires ShellCheck and shfmt. CI includes both plus Linux unit-syntax checks and Python 3.11/3.13. No live LXC, downloads, real JVM/world, Playit tunnel or external player login has been executed during this development; see [validation and acceptance steps](docs/VALIDATION.md).

Installer code is MIT licensed. Minecraft requires its own EULA/license and a compatible licensed client; its binaries are not redistributed. Fabric and Eclipse Temurin retain their respective licenses. [Research and attribution](docs/RESEARCH.md) records the official sources and limits of verification.
