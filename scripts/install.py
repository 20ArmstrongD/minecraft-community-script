#!/usr/bin/env python3
"""Install into an existing Debian LXC; explicit resume/repair, no destructive reruns."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import pwd
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import artifacts
import configuration as cfg
import manage as m

ROOT = Path(__file__).resolve().parent.parent


def ask(label, default):
    value = input(f'{label} [{default}]: ')
    return value if value else default


def wizard():
    c = dict(cfg.DEFAULTS)
    manifest = artifacts.document(artifacts.MANIFEST)
    latest = manifest['latest']['release']
    print('Choose an EXACT version. The suggested release is not automatically upgraded later.')
    c['minecraft_version'] = ask('Minecraft version', latest)
    c['loader'] = ask('Loader: vanilla or fabric', 'vanilla')
    if c['loader'] == 'fabric':
        print('Use the pinned versions listed at https://fabricmc.net/use/server/ for your Minecraft release.')
        c['fabric_loader_version'] = input('Exact Fabric loader version: ')
        c['fabric_installer_version'] = input('Exact Fabric installer version: ')
    c['world_name'] = ask('World directory name (fixed after install)', c['world_name'])
    c['seed'] = input('Optional world seed (numeric or text; blank generates a random seed): ')
    for key, label in [('heap_min_mb', 'Minimum Java heap MiB'), ('heap_max_mb', 'Maximum Java heap MiB'),
                       ('port', 'Minecraft listening TCP port'), ('max_players', 'Maximum players'),
                       ('difficulty', 'Difficulty'), ('game_mode', 'Game mode'), ('whitelist', 'Require whitelist (true/false)'),
                       ('motd', 'Server message'), ('view_distance', 'View distance'), ('simulation_distance', 'Simulation distance')]:
        c[key] = ask(label, c[key])
    c['tunnel_destination_port'] = c['port']
    print('Read the Minecraft EULA at https://aka.ms/MinecraftEULA and https://www.minecraft.net/eula')
    if input('Type ACCEPT to accept the Minecraft EULA: ') != 'ACCEPT':
        raise ValueError('Minecraft EULA not accepted')
    c['eula'] = 'true'
    return cfg.validate(c)


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', type=Path, help='Literal config file; no shell evaluation')
    p.add_argument('--accept-eula', action='store_true', help='Explicit Minecraft EULA acceptance for unattended setup')
    p.add_argument('--yes', action='store_true', help='Unattended installation using --config and --accept-eula')
    p.add_argument('--no-start', action='store_true')
    p.add_argument('--resume', action='store_true', help='Resume THIS installer\'s partial setup with exactly the same choices')
    p.add_argument('--repair', action='store_true', help='Refresh managed scripts/units only; preserve versions, data and config; leave game stopped')
    return p


def platform_check():
    if os.geteuid() != 0:
        raise ValueError('Run as root inside the Debian LXC')
    if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'aarch64'):
        raise ValueError('Requires Debian amd64/arm64 Linux')
    fields = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
    if fields.get('ID', '').strip('"') != 'debian' or fields.get('VERSION_ID', '').strip('"') not in ('12', '13'):
        raise ValueError('Targets Debian 12/13 with systemd')
    if not Path('/run/systemd/system').is_dir():
        raise ValueError('A running systemd guest is required')
    if sys.version_info < (3, 11) or not hasattr(__import__('tarfile'), 'data_filter'):
        raise ValueError('Install current Debian Python security updates (tarfile data_filter required)')


def guard(c, resume=False, repair=False):
    for directory in (m.ETC, m.DATA, m.BACK, m.RELEASES, m.RUNTIMES, m.RELEASES.parent):
        if directory.is_symlink():
            raise ValueError('Managed installation paths must not be symbolic links')
    marker = m.ETC/'installing'
    initialized = (m.ETC/'state.json').exists()
    fingerprint = hashlib.sha256(cfg.serialize(c).encode()).hexdigest() if c else None
    if repair:
        if not initialized or marker.exists():
            raise ValueError('Repair requires a completed managed installation; partial installs require --resume')
        return
    if marker.exists():
        if not resume:
            raise ValueError('Partial installation detected; inspect it, then rerun with --resume and the SAME config/EULA acceptance')
        if json.loads(m.private_file(marker).read_text()).get('config_sha256') != fingerprint:
            raise ValueError('Resume configuration differs from the original choices; no installation files were replaced')
        return
    if resume:
        raise ValueError('No partial installation from this installer exists')
    if initialized or any(path.exists() and (not path.is_dir() or any(path.iterdir())) for path in (m.ETC, m.DATA, m.RELEASES.parent)):
        raise ValueError('Existing server/configuration detected. Installation refuses to overwrite it; use the installed CLI or explicit --repair for managed code.')
    if (m.UNITS/m.SERVICE).exists() or Path('/usr/local/bin/minecraft').exists():
        raise ValueError('Existing Minecraft unit or CLI detected; refusing to overwrite an unmanaged installation')


def install_code():
    target = Path('/usr/local/lib/minecraft-server')
    if target.exists() and (target.is_symlink() or not target.is_dir() or target.stat().st_uid != 0):
        raise ValueError('Managed code directory must be a real root-owned directory')
    target.mkdir(parents=True, exist_ok=True); target.chmod(0o755)
    for name in ('configuration.py', 'artifacts.py', 'manage.py', 'launcher.py', 'probe.py'):
        m.atomic(target/name, (ROOT/'scripts'/name).read_bytes(), 0o644, (0, 0))
    m.atomic(Path('/usr/local/bin/minecraft'), (ROOT/'scripts/minecraft').read_bytes(), 0o755, (0, 0))
    # Debian's root login PATH includes /usr/local/bin. No overwrite of /usr/bin.
    for source in (ROOT/'systemd').glob('*'):
        m.atomic(m.UNITS/source.name, source.read_bytes(), 0o644, (0, 0))
    m.run(['systemctl', 'daemon-reload'])


def install(c, args):
    guard(c, args.resume)
    m.run(['apt-get', 'update'])
    m.run(['apt-get', 'install', '-y', '--no-install-recommends', 'python3', 'ca-certificates', 'iproute2', 'tar', 'gzip', 'util-linux'])
    try:
        identity = pwd.getpwnam('minecraft')
        if identity.pw_dir != str(m.DATA) or identity.pw_shell != '/usr/sbin/nologin' or identity.pw_uid == 0:
            raise ValueError('Existing minecraft account differs from the managed service account; refusing migration')
    except KeyError:
        m.run(['useradd', '--system', '--user-group', '--home-dir', m.DATA, '--shell', '/usr/sbin/nologin', 'minecraft'])
    uid, gid = m.game_identity()
    for path, owner, mode in [(m.ETC, (0, gid), 0o750), (m.DATA, (uid, gid), 0o750),
                              (m.BACK, (0, 0), 0o700), (m.RELEASES, (0, 0), 0o755), (m.RUNTIMES, (0, 0), 0o755)]:
        path.mkdir(parents=True, exist_ok=True); os.chown(path, *owner); path.chmod(mode)
    m.atomic(m.ETC/'installing', json.dumps({'config_sha256': hashlib.sha256(cfg.serialize(c).encode()).hexdigest(), 'phase': 'artifact staging'}), 0o640, (0, gid))
    m.save_config(c)
    m.resources(c); m.port_free(c['port'])
    runtime = artifacts.stage_release(c, m.RELEASES, m.RUNTIMES)
    for name in ('mods', 'config'):
        directory = m.DATA/name
        directory.mkdir(exist_ok=True); os.chown(directory, uid, gid); directory.chmod(0o750)
    cfg.world_guard(m.DATA, None, c)
    install_code()
    m.apply_files(c, runtime)  # seed and world path written before first launch
    m.run(['systemctl', 'enable', m.SERVICE])
    (m.ETC/'installing').unlink()
    print('Installation complete. Whitelist is enabled by default; use minecraft console "whitelist add PLAYER" after startup.')
    if not args.no_start:
        m.start(c)
    print('Existing-agent networking: run minecraft network configure inside this guest. No Playit agent or tunnel was changed.')


def main(argv=None):
    args = parser().parse_args(argv)
    platform_check()
    if args.repair:
        if args.resume or args.config or args.accept_eula or args.yes:
            raise ValueError('--repair refreshes code only and cannot be combined with setup/resume flags')
        guard(None, repair=True)
        with m.lock():
            m.stop(); install_code(); m.calendar(m.load())
        print('Managed code repaired; game stopped, existing data/configuration/versions retained. Run minecraft start explicitly.')
        return
    if args.config:
        c = cfg.parse(args.config.read_text())
        if not args.accept_eula:
            raise ValueError('A config eula=true is not sufficient: explicitly pass --accept-eula after reading the EULA')
        c['eula'] = 'true'
        if not args.yes and (not sys.stdin.isatty() or input('Type INSTALL to install the pinned configuration: ') != 'INSTALL'):
            raise ValueError('Installation cancelled; unattended mode requires --yes')
    else:
        if args.yes or not sys.stdin.isatty():
            raise ValueError('Unattended setup requires --config, --accept-eula and --yes')
        c = wizard()
    with m.lock():
        install(c, args)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError, KeyError, __import__('subprocess').SubprocessError) as error:
        print('installer: '+str(error), file=sys.stderr)
        sys.exit(1)
