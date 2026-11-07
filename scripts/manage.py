#!/usr/bin/env python3
"""Minecraft lifecycle, configuration, maintenance and existing-agent networking."""
import argparse
from contextlib import contextmanager
import datetime
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile

# -I ignores PYTHONPATH and cwd; load only our root-owned installed modules.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import artifacts
import configuration as cfg
import probe
from launcher import command as console_command

VERSION = '0.1.0'
ETC = Path('/etc/minecraft-server')
DATA = Path('/var/lib/minecraft-server')
BACK = Path('/var/backups/minecraft-server')
RELEASES = Path('/opt/minecraft-server/releases')
RUNTIMES = Path('/opt/minecraft-server/java')
UNITS = Path('/etc/systemd/system')
LOCK = Path('/run/lock/minecraft-server.lock')
CONSOLE = Path('/run/minecraft-server/console.sock')
SERVICE = 'minecraft-server.service'
ROOT = Path(__file__).resolve().parent.parent


def run(args, check=True):
    try:
        result = subprocess.run([str(a) for a in args], text=True, capture_output=True)
    except FileNotFoundError:
        raise ValueError(f'Missing dependency: {args[0]}') from None
    if check and result.returncode:
        raise RuntimeError(f'{args[0]} failed (status {result.returncode}). Inspect its service journal/output locally; no server data was erased.')
    return result


def atomic(path, value, mode=0o640, owner=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix='.'+path.name+'.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(value.encode('utf-8') if isinstance(value, str) else value)
            stream.flush(); os.fsync(stream.fileno())
        os.chmod(name, mode)
        if owner is not None:
            os.chown(name, *owner)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def game_identity():
    identity = pwd.getpwnam('minecraft')
    return identity.pw_uid, identity.pw_gid


def private_file(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) not in (0o600, 0o640):
        raise ValueError(f'{path.name} must be a regular root-owned file, mode 0600 or 0640, without group write/world access')
    return path


def load():
    return cfg.parse(private_file(ETC/'config.env').read_text())


def state():
    return json.loads(private_file(ETC/'state.json').read_text())


def save_state(value):
    atomic(ETC/'state.json', json.dumps(value, indent=2)+'\n', owner=(0, game_identity()[1]))


def save_config(c):
    atomic(ETC/'config.env', cfg.serialize(c), owner=(0, game_identity()[1]))


@contextmanager
def lock():
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(LOCK, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(descriptor, 'a') as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise ValueError('Maintenance lock must be a regular file owned by the current administrator')
        os.fchmod(stream.fileno(), 0o600)
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another Minecraft operation is running; wait for it to complete') from None
        yield


def active():
    return run(['systemctl', 'is-active', '--quiet', SERVICE], False).returncode == 0


def stop():
    run(['systemctl', 'stop', SERVICE])
    # is-active alone can say failed although its process/cgroup still exists.
    properties = run(['systemctl', 'show', SERVICE, '--property=MainPID,ControlGroup,ActiveState,SubState']).stdout
    values = dict(line.split('=', 1) for line in properties.splitlines() if '=' in line)
    if values.get('MainPID') != '0' or values.get('ActiveState') not in ('inactive', 'failed'):
        raise RuntimeError('Shutdown incomplete. No backup/update/restore is safe; inspect the journal and wait for saves. No SIGKILL was sent.')
    group = values.get('ControlGroup', '')
    if group:
        path = Path('/sys/fs/cgroup')/group.lstrip('/')
        if path.exists():
            for procs in path.rglob('cgroup.procs'):
                if procs.read_text().strip():
                    raise RuntimeError('Processes remain in the game service cgroup; refusing maintenance')


def port_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as stream:
        try:
            stream.bind(('0.0.0.0', int(port)))
        except OSError:
            raise ValueError(f'TCP {port} is occupied; stop the conflicting service or choose a different game port') from None


def memory_limit():
    # cgroup v2 plus v1 for existing older guests; host MemTotal is not the LXC limit.
    candidates = []
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            if line.startswith('MemTotal:'):
                candidates.append(int(line.split()[1])*1024)
        for name in ('/sys/fs/cgroup/memory.max', '/sys/fs/cgroup/memory/memory.limit_in_bytes'):
            if Path(name).exists():
                value = Path(name).read_text().strip()
                if value != 'max':
                    candidates.append(int(value))
    except (OSError, ValueError):
        pass
    return min(candidates) if candidates else None


def resources(c):
    limit = memory_limit()
    if limit and int(c['heap_max_mb'])*1024**2 + 1024**3 > limit:
        raise ValueError('Java max heap needs at least 1 GiB additional container RAM for native memory/OS. Increase LXC RAM or reduce heap_max_mb.')


def matches(c, installed):
    return all(c[k] == installed['runtime']['versions'][k] for k in ('loader', 'minecraft_version', 'fabric_loader_version', 'fabric_installer_version'))


def start(c):
    if any((ETC/name).exists() for name in ('maintenance', 'installing', 'restore-required')):
        raise ValueError('Maintenance/partial install blocks startup. Use minecraft recover only after inspecting the recorded failure.')
    current = state()
    if c != current['applied']:
        raise ValueError('Configuration has unapplied changes. Run minecraft config apply; version changes require minecraft update.')
    if c['eula'] != 'true':
        raise ValueError('Explicit EULA acceptance required')
    resources(c)
    if not active():
        port_free(c['port'])
    run(['systemctl', 'start', SERVICE])
    print('Service start requested. Use minecraft info/logs for actual Minecraft readiness; a running systemd process is not proof of a player join.')


def tree_check(root):
    if root.is_symlink():
        raise ValueError('Managed data root must not be a symlink')
    for path in root.rglob('*'):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError('Data contains symlink/special files; replace them with real files before backup')


def checksum(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')


def check_archive(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or not path.name.endswith('.tar.gz'):
        raise ValueError('Choose a regular .tar.gz backup archive, not a directory or symlink')
    expected_file = path.with_name(path.name+'.sha256')
    if expected_file.is_symlink() or not expected_file.is_file() or expected_file.read_text().strip() != checksum(path):
        raise ValueError('Backup SHA-256 missing or mismatched')
    with tarfile.open(path, 'r:gz') as tar:
        members = tar.getmembers()
        names = set(); total = 0
        if len(members) > 500000:
            raise ValueError('Backup has too many files')
        for member in members:
            part = PurePosixPath(member.name)
            if part.is_absolute() or '..' in part.parts or not part.parts or part.parts[0] not in ('data', 'config.env', 'desired.env', 'state.json', 'backup.json') or not (member.isdir() or member.isfile()) or member.name in names:
                raise ValueError('Unsafe/duplicate backup path or unsupported file type')
            if part.parts[0] != 'data' and (len(part.parts) != 1 or not member.isfile()):
                raise ValueError('Invalid backup metadata member')
            names.add(member.name); total += member.size
            if member.isfile():
                with tar.extractfile(member) as stream:
                    while stream.read(1024*1024):
                        pass  # verifies gzip/tar payload, not just metadata
        if not {'data', 'config.env', 'state.json', 'backup.json'} <= names:
            raise ValueError('Backup missing required data/configuration/provenance')
        return total


def snapshot(c, prune=True):
    """Caller owns lock, maintenance marker and stopped-service proof."""
    tree_check(DATA)
    BACK.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = BACK/('minecraft-'+stamp()+'.tar.gz')
    temporary = target.with_name('.'+target.name+'.part')
    try:
        with tempfile.TemporaryDirectory(dir=BACK) as tmp:
            metadata = Path(tmp)/'backup.json'
            atomic(metadata, json.dumps({'format': 1, 'created_utc': stamp(), 'versions': state()['runtime']['versions'], 'note': 'Restore world and matching version together; binary downgrade alone is unsafe.'}), 0o600)
            with tarfile.open(temporary, 'w:gz') as tar:
                tar.add(DATA, arcname='data', recursive=True)
                atomic(Path(tmp)/'config.env', cfg.serialize(state()['applied']), 0o600)
                tar.add(Path(tmp)/'config.env', arcname='config.env')
                tar.add(ETC/'config.env', arcname='desired.env')
                tar.add(ETC/'state.json', arcname='state.json')
                tar.add(metadata, arcname='backup.json')
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
        atomic(target.with_name(target.name+'.sha256'), checksum(target)+'\n', 0o600)
        check_archive(target)
        if prune:
            for old in sorted(BACK.glob('minecraft-*.tar.gz'), reverse=True)[int(c['backup_retention']):]:
                old.unlink(); old.with_name(old.name+'.sha256').unlink(missing_ok=True)
        print('Backup verified: '+str(target))
        return target
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def maintenance(reason, resume=True):
    marker = ETC/'maintenance'
    if marker.exists():
        raise ValueError('Previous maintenance is incomplete; inspect minecraft info and recover before retrying')
    was_running = active()
    atomic(marker, json.dumps({'operation': reason, 'started': stamp()}), 0o640, (0, game_identity()[1]))
    try:
        stop()
        yield
    except BaseException:
        print('Operation failed. Game remains stopped/inhibited; existing data and recovery evidence are retained. Read minecraft info, then minecraft recover --help.', file=sys.stderr)
        raise
    else:
        marker.unlink()
        if resume and was_running:
            start(load())


def backup(c, scheduled=False):
    if scheduled and c['backup_enabled'] == 'false':
        print('Scheduled backups disabled by configuration'); return
    with maintenance('backup'):
        snapshot(c)


def calendar(c):
    run(['systemd-analyze', 'calendar', c['backup_calendar']])
    target = UNITS/'minecraft-backup.timer.d/schedule.conf'
    atomic(target, '[Timer]\nOnCalendar=\nOnCalendar='+c['backup_calendar']+'\n', 0o644)
    run(['systemctl', 'daemon-reload'])
    run(['systemctl', 'enable' if c['backup_enabled'] == 'true' else 'disable', '--now', 'minecraft-backup.timer'])
    if c['backup_enabled'] == 'true':
        run(['systemctl', 'restart', 'minecraft-backup.timer'])


def apply_files(c, runtime):
    properties = DATA/'server.properties'
    old = properties.read_text(encoding='iso-8859-1') if properties.exists() else ''
    uid, gid = game_identity()
    atomic(properties, cfg.properties(old, c).encode('iso-8859-1'), 0o640, (uid, gid))
    atomic(DATA/'eula.txt', 'eula=true\n', 0o640, (uid, gid))
    save_state({'format': 1, 'applied': c, 'runtime': runtime})
    calendar(c)


def config_apply(c):
    current = state()
    if not matches(c, current):
        raise ValueError('Version/loader changes require minecraft update with explicit version flags; not config apply')
    cfg.world_guard(DATA, current['applied'], c)
    if c['eula'] != 'true':
        raise ValueError('eula must remain explicitly accepted')
    resources(c)
    if c == current['applied'] and (DATA/'server.properties').exists() and (DATA/'server.properties').read_text(encoding='iso-8859-1') == cfg.properties((DATA/'server.properties').read_text(encoding='iso-8859-1'), c):
        print('Configuration already applied; no restart'); return
    with maintenance('config apply'):
        snapshot(current['applied'], prune=False)
        port_free(c['port'])
        # Save exact prior managed files for a recoverable failed apply.
        recovery = ETC/'apply-recovery'
        if recovery.exists():
            raise ValueError('Existing apply recovery requires inspection')
        recovery.mkdir(mode=0o700)
        for path in (ETC/'state.json', DATA/'server.properties'):
            if path.exists():
                shutil.copy2(path, recovery/path.name)
        try:
            apply_files(c, current['runtime'])
        except BaseException:
            for path in (ETC/'state.json', DATA/'server.properties'):
                if (recovery/path.name).exists():
                    atomic(path, (recovery/path.name).read_bytes(), 0o640, (0 if path.parent == ETC else game_identity()[0], game_identity()[1]))
            shutil.rmtree(recovery)
            raise
        else:
            shutil.rmtree(recovery)
    print('Managed properties applied; unrelated properties and worlds retained.')


def update(c, args):
    if (ETC/'maintenance').exists():
        raise ValueError('Incomplete maintenance must be recovered before staging another update')
    current = state()
    proposed = dict(c)
    for key in ('minecraft_version', 'fabric_loader_version', 'fabric_installer_version', 'loader'):
        value = getattr(args, key, None)
        if value is not None:
            proposed[key] = value
    if proposed['loader'] == 'vanilla':
        proposed['fabric_loader_version'] = proposed['fabric_installer_version'] = ''
    cfg.validate(proposed); resources(proposed)
    cfg.world_guard(DATA, current['applied'], proposed)
    if proposed['eula'] != 'true':
        raise ValueError('EULA acceptance is required')
    if not args.minecraft_version and not args.fabric_loader_version and not args.fabric_installer_version and not args.loader:
        raise ValueError('Explicit version/loader flags are required; no automatic latest upgrade')
    print('Target pins: '+json.dumps({key: proposed[key] for key in current['runtime']['versions']}))
    print('WARNING: Check all Fabric mods/dependencies before changing versions. A binary downgrade alone cannot safely downgrade a world.')
    if not args.yes and (not sys.stdin.isatty() or input('Type UPDATE to back up and install exactly these versions: ') != 'UPDATE'):
        raise ValueError('Update cancelled; automation requires --yes')
    # Staging downloads does not alter the installed world/config/service.
    runtime = artifacts.stage_release(proposed, RELEASES, RUNTIMES)
    # Release timestamps from Mojang provide chronological rather than lexicographic ordering.
    old_meta = artifacts.game_metadata(current['runtime']['versions']['minecraft_version'])
    new_meta = artifacts.game_metadata(proposed['minecraft_version'])
    downgrade = new_meta['releaseTime'] < old_meta['releaseTime']
    if downgrade and not args.allow_downgrade:
        raise ValueError('World downgrade refused. Restore a matching pre-upgrade world; --allow-downgrade --no-start is only for preparing that restore.')
    if args.allow_downgrade and not args.no_start:
        raise ValueError('--allow-downgrade requires --no-start; do not open a newer world with older binaries')
    with maintenance('update', resume=not args.no_start):
        snapshot(current['applied'], prune=False)
        # Preserve old cache rather than deleting it; release changes may invalidate remapping.
        cache = DATA/'.fabric'
        if cache.exists():
            os.replace(cache, DATA/('.fabric.previous-'+stamp()))
        previous = {path: path.read_bytes() if path.exists() else None for path in
                    (ETC/'config.env', ETC/'state.json', DATA/'server.properties', ETC/'restore-required')}
        try:
            if downgrade:
                atomic(ETC/'restore-required', 'Older binaries staged: restore their matching backup before starting.\n', 0o640, (0, game_identity()[1]))
            save_config(proposed)
            apply_files(proposed, runtime)
        except BaseException:
            for path, content in previous.items():
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic(path, content, 0o640, (game_identity()[0] if path.parent == DATA else 0, game_identity()[1]))
            calendar(current['applied'])
            raise
    print('Exact requested versions installed; prior release and verified backup retained.')


def restore(c, args):
    path = args.archive.absolute()
    size = check_archive(path)
    if size > shutil.disk_usage(DATA.parent).free:
        raise ValueError('Insufficient free disk space to stage this restore')
    if not args.yes and (not sys.stdin.isatty() or input('Type RESTORE to replace current server data with the selected backup: ') != 'RESTORE'):
        raise ValueError('Restore cancelled; automation requires --yes')
    with tempfile.TemporaryDirectory(prefix='.restore-', dir=DATA.parent) as name:
        stage = Path(name)
        with tarfile.open(path, 'r:gz') as tar:
            tar.extractall(stage, filter='data')
        candidate = cfg.parse((stage/'config.env').read_text())
        old_state = json.loads((stage/'state.json').read_text())
        if candidate != old_state['applied']:
            raise ValueError('Backup contains unapplied configuration. Correct config/apply before backup, or recover manually in a disposable guest.')
        installed = state()
        if not matches(candidate, installed):
            raise ValueError('Backup version pins differ from installed runtime. Prepare its exact versions with minecraft update --no-start (and explicit --allow-downgrade if needed), then restore before starting.')
        if candidate['eula'] != 'true' or json.loads((stage/'backup.json').read_text()).get('format') != 1:
            raise ValueError('Unsupported backup format or EULA not accepted')
        # Numeric ownership from untrusted archives is not accepted.
        uid, gid = game_identity()
        for item in [stage/'data'] + list((stage/'data').rglob('*')):
            os.chown(item, uid, gid)
            item.chmod(0o750 if item.is_dir() else 0o640)
        rollback = DATA.with_name(DATA.name+'.restore-rollback')
        if rollback.exists():
            raise ValueError('Interrupted restore directory exists; inspect it before recovery')
        with maintenance('restore', resume=False):
            snapshot(c, prune=False)
            previous = (ETC/'config.env').read_bytes(), (ETC/'state.json').read_bytes()
            os.replace(DATA, rollback)
            try:
                os.replace(stage/'data', DATA)
                save_config(candidate)
                # Keep the locally verified matching runtime paths, never execute paths from the archive.
                apply_files(candidate, installed['runtime'])
            except BaseException:
                if DATA.exists():
                    shutil.rmtree(DATA)
                os.replace(rollback, DATA)
                for target, content in zip((ETC/'config.env', ETC/'state.json'), previous):
                    atomic(target, content, 0o640, (0, gid))
                raise
            else:
                shutil.rmtree(rollback)
                (ETC/'restore-required').unlink(missing_ok=True)
    print('Restore verified and committed. Game left stopped; inspect configuration, then minecraft start.')


def console(value):
    value = console_command(value)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(5); connection.connect(str(CONSOLE))
        connection.sendall(value.encode('utf-8')+b'\n')
        print(connection.recv(4096).decode('utf-8').strip())


def local_addresses():
    data = json.loads(run(['ip', '-j', '-4', 'addr', 'show']).stdout)
    return sorted({str(ipaddress.IPv4Address(item['local'])) for interface in data
                   if interface.get('ifname') != 'lo'
                   for item in interface.get('addr_info', []) if item.get('family') == 'inet' and item.get('scope') == 'global'})


def network(c, args):
    if args.network_action == 'test':
        host, port = ('127.0.0.1', int(c['port']))
        if args.public:
            target = cfg.endpoint(c['public_endpoint'])
            if not target:
                raise ValueError('Public endpoint not configured')
            host, port = target
        result = probe.status(host, port)
        print(json.dumps({'from': 'this guest', 'endpoint': f'{host}:{port}', 'minecraft_status': result,
                          'agent_to_guest': 'UNVERIFIED; run probe.py on the existing agent host', 'player_join': 'UNVERIFIED'}))
        return
    new = dict(c)
    interactive = not args.yes
    values = {'agent_location': args.agent_location, 'tunnel_destination_ip': args.destination_ip, 'public_endpoint': args.public_endpoint}
    if interactive and not sys.stdin.isatty():
        raise ValueError('Noninteractive network configuration requires --yes and all endpoint flags')
    addresses = local_addresses()
    print('Existing Playit agent only: no installation, credential access or tunnel edits.')
    print('Create a SEPARATE Minecraft Java TCP tunnel; Proxy Protocol OFF. Do not change other game tunnels.')
    for key, supplied in values.items():
        if supplied is None and interactive:
            label = {'agent_location': 'Where does the existing agent run? (machine/guest and LAN/VPN relationship)',
                     'tunnel_destination_ip': 'Reachable IPv4 of THIS Minecraft guest: '+', '.join(addresses),
                     'public_endpoint': 'Assigned public hostname/IP:TCP_PORT from the new Playit tunnel'}[key]
            supplied = input(label+': ')
        if supplied is None or not supplied:
            raise ValueError('Agent location, guest destination and actual public endpoint are required')
        new[key] = supplied
    new['tunnel_destination_port'] = new['port']
    cfg.validate(new)
    if new['tunnel_destination_ip'] not in addresses:
        raise ValueError('Destination IP is not assigned to this guest. Do not use the agent machine address or localhost.')
    print(f'Tunnel destination: {new["tunnel_destination_ip"]}:{new["port"]}; public: {new["public_endpoint"]} (TCP ports may differ)')
    if interactive and input('Type SAVE to record these dashboard values: ') != 'SAVE':
        raise ValueError('Cancelled; no settings changed')
    # Networking metadata is deliberately not a game restart/config apply.
    current = state()
    for key in ('agent_location', 'tunnel_destination_ip', 'tunnel_destination_port', 'public_endpoint'):
        current['applied'][key] = new[key]
    save_config(new); save_state(current)
    print('Endpoint saved. Reachability remains UNVERIFIED. Run the supplied probe.py from the agent host, then join from an external Minecraft Java client.')


def info(c):
    current = state()
    print('Minecraft manager '+VERSION)
    print('Installed versions: '+json.dumps(current['runtime']['versions']))
    print('Java: '+json.dumps(current['runtime']['provenance']['java']))
    print('Service active: '+str(active()))
    print('Guest IPv4: '+', '.join(local_addresses())+'; listening TCP '+c['port'])
    print('Configured public endpoint: '+(c['public_endpoint'] or 'not configured'))
    print('Shared agent: '+(c['agent_location'] or 'not recorded')+'; availability/route UNVERIFIED')
    print('Effective RAM MiB: '+str(memory_limit()//1024**2 if memory_limit() else 'unknown'))
    print('Free data disk GiB: '+str(round(shutil.disk_usage(DATA).free/1024**3, 1)))
    print('Unapplied configuration: '+str(c != current['applied']))
    for name in ('maintenance', 'installing', 'restore-required'):
        print(name+': '+((ETC/name).read_text() if (ETC/name).exists() else 'none'))
    try:
        print('Minecraft local status: PASS '+json.dumps(probe.status('127.0.0.1', int(c['port']))))
    except (OSError, ValueError):
        print('Minecraft local status: FAIL/STOPPED. Inspect minecraft logs; service status alone is not readiness.')
    print('External tunnel/client login: UNVERIFIED. No backend or player-join success is inferred.')


def parser():
    p = argparse.ArgumentParser(prog='minecraft', description=__doc__)
    actions = p.add_subparsers(dest='action', required=True)
    for action in ('start', 'stop', 'restart', 'status', 'info'):
        actions.add_parser(action)
    logs = actions.add_parser('logs'); logs.add_argument('--tail', type=int, default=100); logs.add_argument('--no-follow', action='store_true')
    con = actions.add_parser('console'); con.add_argument('command', nargs='?', help='Quote one Minecraft command; omit for interactive console. No RCON is enabled.')
    back = actions.add_parser('backup'); back.add_argument('--scheduled', action='store_true')
    restore_parser = actions.add_parser('restore'); restore_parser.add_argument('archive', type=Path); restore_parser.add_argument('--yes', action='store_true')
    up = actions.add_parser('update'); up.add_argument('--minecraft-version'); up.add_argument('--loader', choices=('vanilla', 'fabric')); up.add_argument('--fabric-loader-version'); up.add_argument('--fabric-installer-version'); up.add_argument('--yes', action='store_true'); up.add_argument('--no-start', action='store_true'); up.add_argument('--allow-downgrade', action='store_true')
    config = actions.add_parser('config'); ca = config.add_subparsers(dest='config_action', required=True); ca.add_parser('check'); ca.add_parser('show'); ca.add_parser('apply')
    recover = actions.add_parser('recover', help='Clear a failed maintenance inhibit after manual inspection; never modifies worlds')
    recover.add_argument('--yes', action='store_true')
    recover.add_argument('--discard-pending-config', action='store_true', help='Explicitly restore desired config from last applied state; never changes worlds')
    net = actions.add_parser('network'); na = net.add_subparsers(dest='network_action', required=True)
    nc = na.add_parser('configure'); nc.add_argument('--agent-location'); nc.add_argument('--destination-ip'); nc.add_argument('--public-endpoint'); nc.add_argument('--yes', action='store_true')
    nt = na.add_parser('test'); nt.add_argument('--public', action='store_true')
    actions.add_parser('version')
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    if args.action == 'version':
        print(VERSION); return 0
    if os.geteuid() != 0:
        raise ValueError('Run minecraft as root inside the LXC (sudo minecraft ...)')
    if args.action == 'logs':
        if not 1 <= args.tail <= 10000:
            raise ValueError('--tail must be 1..10000')
        os.execvp('journalctl', ['journalctl', '-u', SERVICE, '-n', str(args.tail), '--no-pager'] + ([] if args.no_follow else ['-f']))
    c = None if args.action == 'stop' else cfg.validate(state()['applied']) if args.action == 'recover' and args.discard_pending_config else load()
    if args.action in ('info', 'status'):
        info(c)
        return 0 if args.action == 'info' or active() else 3
    if args.action == 'console':
        if args.command:
            console(args.command)
        elif sys.stdin.isatty():
            print('Console commands only; Ctrl+D exits this client without stopping the server. Responses appear in minecraft logs.')
            try:
                while True:
                    console(input('minecraft> '))
            except EOFError:
                pass
        else:
            raise ValueError('Provide a quoted console command or use an interactive terminal')
        return 0
    with lock():
        if args.action == 'start': start(c)
        elif args.action == 'stop': stop()
        elif args.action == 'restart': stop(); start(c)
        elif args.action == 'backup': backup(c, args.scheduled)
        elif args.action == 'restore': restore(c, args)
        elif args.action == 'update': update(c, args)
        elif args.action == 'network': network(c, args)
        elif args.action == 'config':
            if args.config_action == 'apply': config_apply(c)
            elif args.config_action == 'show': print(cfg.serialize(c), end='')
            else: print('Configuration valid; run config apply to explicitly apply game settings.')
        elif args.action == 'recover':
            if (ETC/'installing').exists():
                raise ValueError('Incomplete initial install: rerun install.sh --resume with the same configuration; recover cannot bypass it')
            if (ETC/'apply-recovery').exists() or DATA.with_name(DATA.name+'.restore-rollback').exists():
                raise ValueError('Recovery files need inspection before clearing maintenance; see README recovery instructions')
            if not args.yes and (not sys.stdin.isatty() or input('Type RECOVER after checking the failure, data and configuration: ') != 'RECOVER'):
                raise ValueError('Recovery cancelled; automation requires --yes')
            stop()
            if args.discard_pending_config:
                c = cfg.validate(state()['applied'])
                save_config(c)
                print('Pending management config discarded; last applied choices restored. Worlds unchanged.')
            cfg.world_guard(DATA, state()['applied'], c)
            if c != state()['applied']:
                raise ValueError('Unapplied config differs from the last committed state; restore it before recovery')
            (ETC/'maintenance').unlink(missing_ok=True)
            print('Maintenance inhibit cleared. Game remains stopped; start it explicitly when ready.')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, RuntimeError, OSError, KeyError, tarfile.TarError, subprocess.SubprocessError) as error:
        print('minecraft: '+str(error), file=sys.stderr)
        sys.exit(1)
