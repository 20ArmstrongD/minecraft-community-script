#!/usr/bin/env python3
"""Optional standalone Proxmox wizard: creates NEW unprivileged guests only."""
import argparse
import ipaddress
import os
import platform
from pathlib import Path
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import configuration as cfg
import install as guest
from manage import run

ROOT = Path(__file__).resolve().parent.parent


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--template', required=True, help='Existing trusted Debian 12/13 amd64 or arm64 template volume ID')
    p.add_argument('--config', type=Path, help='Full server config for unattended setup')
    p.add_argument('--accept-eula', action='store_true'); p.add_argument('--yes', action='store_true')
    p.add_argument('--ctid', type=int); p.add_argument('--hostname', default='minecraft')
    p.add_argument('--storage', default='local-lvm'); p.add_argument('--bridge', default='vmbr0')
    p.add_argument('--cores', type=int, default=4); p.add_argument('--ram', type=int, default=8192)
    p.add_argument('--swap', type=int, default=1024); p.add_argument('--disk', type=int, default=40)
    p.add_argument('--ipv4', default='dhcp'); p.add_argument('--gateway', default='')
    p.add_argument('--vlan', type=int, default=0); p.add_argument('--dns', default='')
    p.add_argument('--no-start', action='store_true', help='Install but leave game stopped; guest still starts for installation')
    return p


def validate(a):
    if a.ctid is None or not 100 <= a.ctid <= 999999999:
        raise ValueError('Specify an unused --ctid between 100 and 999999999')
    for key in ('hostname', 'storage', 'bridge'):
        if not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]{0,62}', getattr(a, key)):
            raise ValueError('Invalid '+key)
    for key, lo, hi in [('cores', 1, 128), ('ram', 2048, 1048576), ('swap', 0, 1048576), ('disk', 10, 65536), ('vlan', 0, 4094)]:
        if not lo <= getattr(a, key) <= hi:
            raise ValueError(f'{key} must be {lo}..{hi}')
    if a.ipv4 != 'dhcp':
        address = ipaddress.IPv4Interface(a.ipv4)
        gateway = ipaddress.IPv4Address(a.gateway)
        if gateway not in address.network:
            raise ValueError('Static gateway must be on the guest IPv4 subnet')
    elif a.gateway:
        raise ValueError('DHCP does not accept a static gateway')
    if a.dns:
        ipaddress.IPv4Address(a.dns)
    if not re.fullmatch(r'[A-Za-z0-9_.-]+:vztmpl/debian-(12|13)-[A-Za-z0-9_.-]+_(amd64|arm64)\.tar\.(zst|xz|gz)', a.template):
        raise ValueError('Use an existing Debian 12/13 amd64/arm64 template volume ID')
    return a


def create_args(a):
    validate(a)
    net = f'name=eth0,bridge={a.bridge},ip={a.ipv4},firewall=1'
    if a.gateway: net += ',gw='+a.gateway
    if a.vlan: net += ',tag='+str(a.vlan)
    command = ['pct', 'create', str(a.ctid), a.template, '--unprivileged', '1', '--hostname', a.hostname,
               '--cores', str(a.cores), '--memory', str(a.ram), '--swap', str(a.swap),
               '--rootfs', f'{a.storage}:{a.disk}', '--net0', net, '--onboot', '1']
    if a.dns: command += ['--nameserver', a.dns]
    return command


def wizard(a):
    print('Minecraft Java — create a NEW unprivileged Proxmox container')
    print('Defaults assume up to 10 players, light mods, view distance 10, simulation distance 6. Fast single-core CPU and SSD storage help; heavy farms/modpacks need more resources.')
    if a.ctid is None:
        a.ctid = int(run(['pvesh', 'get', '/cluster/nextid']).stdout.strip())
    for key in ('ctid', 'hostname', 'storage', 'bridge', 'cores', 'ram', 'swap', 'disk', 'ipv4'):
        value = guest.ask(key, str(getattr(a, key)))
        setattr(a, key, int(value) if key in ('ctid', 'cores', 'ram', 'swap', 'disk') else value)
    if a.ipv4 != 'dhcp': a.gateway = guest.ask('IPv4 gateway', a.gateway)
    a.vlan = int(guest.ask('VLAN (0 none)', str(a.vlan)))
    a.dns = guest.ask('DNS IPv4 (blank inherit)', a.dns)
    return a


def main(argv=None):
    a = parser().parse_args(argv)
    if os.geteuid() != 0:
        raise ValueError('Run ct/minecraft.sh as root on the Proxmox host')
    if platform.machine() != 'x86_64':
        raise ValueError('The Proxmox host creation wrapper targets official amd64 Proxmox VE hosts')
    if not re.match(r'pve-manager/[89]\.', run(['pveversion']).stdout):
        raise ValueError('Targets Proxmox VE 8/9; other versions are not validated')
    if not a.yes:
        if not sys.stdin.isatty(): raise ValueError('Unattended host setup requires --yes, --ctid, --config and --accept-eula')
        a = wizard(a)
    validate(a)
    if '_amd64.tar.' not in a.template:
        raise ValueError('Use an amd64 template on the targeted Proxmox host; arm64 is guest-installer-only')
    if a.config:
        if not a.accept_eula:
            raise ValueError('--config requires explicit --accept-eula')
        c = cfg.parse(a.config.read_text()); c['eula'] = 'true'
    elif a.yes:
        raise ValueError('Unattended host setup requires --config')
    else:
        c = guest.wizard()
    if int(c['heap_max_mb']) + 1024 > a.ram:
        raise ValueError('Container RAM must exceed Java max heap by at least 1024 MiB')
    # Cluster nextid is advisory; pct create performs the authoritative uniqueness check.
    for kind in ('pct', 'qm'):
        if run([kind, 'config', a.ctid], False).returncode == 0:
            raise ValueError('Guest ID already exists. No existing container or VM was changed.')
    print(f'Create CT {a.ctid}: {a.cores} cores, {a.ram} MiB, {a.disk} GiB, unprivileged, no nesting/keyctl/Docker.')
    if not a.yes and input('Type CREATE to create this new guest: ') != 'CREATE':
        raise ValueError('Cancelled')
    created = False
    try:
        run(create_args(a)); created = True
        run(['pct', 'start', a.ctid])
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            config = path/'config.env'; config.write_text(cfg.serialize(c)); config.chmod(0o600)
            archive = path/'project.tar'
            run(['tar', '-cf', archive, '-C', ROOT, 'scripts', 'systemd', 'install'])
            run(['pct', 'push', a.ctid, archive, '/root/minecraft-project.tar', '--perms', '0600'])
            run(['pct', 'push', a.ctid, config, '/root/minecraft-setup.env', '--perms', '0600'])
        run(['pct', 'exec', a.ctid, '--', 'mkdir', '-p', '/root/minecraft-project'])
        run(['pct', 'exec', a.ctid, '--', 'tar', '-xf', '/root/minecraft-project.tar', '-C', '/root/minecraft-project'])
        # Minimal templates may lack Python; install from Debian's signed repositories.
        run(['pct', 'exec', a.ctid, '--', 'apt-get', 'update'])
        run(['pct', 'exec', a.ctid, '--', 'apt-get', 'install', '-y', '--no-install-recommends', 'python3', 'ca-certificates'])
        args = ['pct', 'exec', a.ctid, '--', 'bash', '/root/minecraft-project/install/minecraft-install.sh',
                '--config', '/root/minecraft-setup.env', '--accept-eula', '--yes']
        if a.no_start: args += ['--no-start']
        run(args)
        print(f'Guest installed. Run pct enter {a.ctid}, then minecraft info and minecraft network configure. Client/tunnel connectivity still needs testing.')
    except BaseException:
        if created:
            print(f'Partial setup retained in CT {a.ctid}. No guest was deleted. Inspect /root/minecraft-project and use the guest installer --resume with the same config.', file=sys.stderr)
        raise


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        print('host installer: '+str(error), file=sys.stderr)
        sys.exit(1)
