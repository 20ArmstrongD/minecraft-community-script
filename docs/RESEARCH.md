# Research and architecture record — 2026-11-07

## Reviewed references

Accessible local checkouts of [Valheim](https://github.com/20ArmstrongD/valheim-community-script) and [Astroneer](https://github.com/20ArmstrongD/astoneer-community-script) were read, including their entrypoints, configuration parsers, lifecycle management, stopped backup/recovery, host wizard and CI conventions. No implementation was assumed from an inaccessible repository and no reference code was modified.

Valheim uses Community Scripts Core to create guests and a native non-root service. Astroneer has a standalone host wizard, configuration-aware Python management and separate networking infrastructure. This project retains the useful CLI, private data-config, maintenance-inhibition and backup conventions. It chooses a standalone reviewed-checkout host wizard so it does not execute a mutable remote Core bootstrap or claim upstream integration. No code was copied from either repository. A future upstream contribution needs the then-current Core/metadata workflow.

## Minecraft, Java and EULA

- [Official server download](https://www.minecraft.net/en-us/download/server): Java server JAR and EULA requirement. No server binary is redistributed.
- [Mojang version manifest](https://piston-meta.mojang.com/mc/game/version_manifest_v2.json): exact release ID, per-version metadata URL and SHA-1. Per-version metadata supplies `javaVersion.majorVersion`, `downloads.server.url`, `sha1`, `size` and release chronology. Missing/new unsupported Java majors fail closed. The code supports majors 17/21/25 and stable releases only; it does not guess requirements from an old tutorial.
- [Official Minecraft 26.1 notes](https://www.minecraft.net/en-us/article/minecraft-java-edition-26-1): explicitly requires Java 25. New storage layouts are another reason to back up the entire working data directory rather than hardcode dimension paths.
- [EULA](https://www.minecraft.net/eula): explicit acceptance is required through a prompt or `--accept-eula`; editable config alone is not setup consent.
- [Official 1.17 release notes](https://www.minecraft.net/en-us/article/caves---cliffs--part-i-out-today-java): introduced `playersSleepingPercentage`. The README command targets its explicit 1.21.1 example, not all future releases. Gamerule names changed in [1.21.11](https://www.minecraft.net/en-us/article/minecraft-java-edition-1-21-11); use version-specific command completion.

The shell cannot currently resolve download hosts, so the live manifest and binary transfers were not exercised locally. The official release notes were available via browsing. Tests supply synthetic payloads and computed hashes; no example hash is claimed to be a real release pin.

## Java distribution

[Eclipse Adoptium's API cookbook](https://github.com/adoptium/api.adoptium.net/blob/main/docs/cookbook.adoc) documents assets metadata, package links and SHA-256 checksums. The installer requests the required major's current GA Linux Temurin JRE for amd64/arm64, validates architecture/OS/major and package hash/size, safely stages its archive, verifies `java -version`, and records the exact build, URL and digest. It does not add an unsigned apt repository or run a remote installation script. Java is resolved only during explicit setup/update operations. Runtime trees are root-owned and not writable by Minecraft.

## Fabric

- [Official installation documentation](https://wiki.fabricmc.net/install): manual server installer CLI accepts `server -dir ... -mcversion ... -loader ...`; versions otherwise default to latest, so this project always supplies both. The `-downloadMinecraft` flag is deliberately omitted: the Minecraft JAR is independently downloaded and hash-verified first.
- [Official Meta endpoint index](https://meta.fabricmc.net/): version-specific loader and server JSON, plus available installer versions. Explicit loader and installer pins are mandatory.
- [Official installer source](https://github.com/FabricMC/fabric-installer/blob/master/src/main/java/net/fabricmc/installer/server/ServerHandler.java) and [ServerInstaller](https://github.com/FabricMC/fabric-installer/blob/master/src/main/java/net/fabricmc/installer/server/ServerInstaller.java): inspected CLI/launcher/library behavior. Installer JAR SHA-256 comes from Fabric Maven. Fetched library paths and SHA-1 checksums are independently checked against the selected server profile and Maven repository before activation. A generated launcher gets a locally recorded SHA-256, not an invented publisher checksum.
- [Official Fabric launcher source](https://github.com/FabricMC/fabric-loader/blob/master/src/main/java/net/fabricmc/loader/impl/launch/server/FabricServerLauncher.java): supports the game-JAR system property; the supervisor supplies an absolute pinned Minecraft JAR path.

The installer runs as the unprivileged Minecraft account and never starts the world. Mods are optional trusted code selected by the operator, not silently installed. Their compatibility, dependencies and client requirements need real testing. Older server or Fabric version combinations lacking the documented metadata/checksum/layout are rejected rather than bypassing verification.

## Existing Playit agent

[Official Playit Minecraft Java guide](https://playit.gg/support/how-to-setup-a-mc-server/) directs tunnel creation to the dashboard's interactive guide. This project leaves provider/account operations manual, selecting the existing agent and adding a separate Minecraft Java TCP tunnel. No provider API, credential copying, agent registration/reset or other-game tunnel update is invented.

Unlike Astroneer's advertised UDP address/port constraints, Minecraft Java accepts a normal TCP tunnel whose public endpoint can use a different port from the internal service. The tunnel's local destination must match the Minecraft guest and listening port. Vanilla/Fabric are not configured for Proxy Protocol. Additional mod protocols (e.g. voice UDP) and Bedrock/Geyser are outside this release.

The game guest cannot independently inspect another machine's agent route or tunnel connection. The standalone status probe must run on the agent's host/network namespace to verify that leg, followed by a real outside-network Java client join. Neither TCP connect nor Minecraft status is authentication/gameplay proof.

## Trust boundaries and limitations

Published Mojang/Fabric SHA-1 values protect consistency with publisher metadata over TLS; SHA-1 is not a modern signature/independent trust anchor. Java/installer SHA-256 and local archive/file hashes likewise do not protect against a compromised publisher. Downloads permit HTTPS and explicit official distribution/CDN hosts only, enforce bounds and reject hash mismatches. No mod binary is redistributed or automatically trusted.

Private local stdin console avoids RCON credentials/network exposure. The supervisor reports Java's actual exit and fails on an unsolicited clean exit. systemd graceful stop sends Minecraft `stop` and refuses unsafe maintenance if the service/cgroup is still alive; no timed forced kill is used. Game readiness, agent route, public status and a player join are reported independently.
