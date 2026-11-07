"""Strict literal key=value configuration and Java .properties handling."""
import ipaddress
import re
from pathlib import Path

DEFAULTS = {
    'loader': 'vanilla', 'minecraft_version': '', 'fabric_loader_version': '',
    'fabric_installer_version': '', 'heap_min_mb': '2048', 'heap_max_mb': '6144',
    'port': '25565', 'world_name': 'world', 'seed': '', 'max_players': '10',
    'difficulty': 'normal', 'game_mode': 'survival', 'whitelist': 'true',
    'motd': 'Minecraft Community Server', 'view_distance': '10', 'simulation_distance': '6',
    'backup_retention': '14', 'backup_enabled': 'true', 'backup_calendar': '*-*-* 05:00:00',
    'eula': 'false', 'public_endpoint': '', 'agent_location': '',
    'tunnel_destination_ip': '', 'tunnel_destination_port': '25565',
}
VERSION = re.compile(r'[0-9][0-9A-Za-z.+_-]{0,63}\Z')
MANAGED = {
    'server-port': 'port', 'level-name': 'world_name', 'level-seed': 'seed',
    'max-players': 'max_players', 'difficulty': 'difficulty', 'gamemode': 'game_mode',
    'white-list': 'whitelist', 'enforce-whitelist': 'whitelist', 'motd': 'motd',
    'view-distance': 'view_distance', 'simulation-distance': 'simulation_distance',
}
FORCED = {'online-mode': 'true', 'enable-rcon': 'false', 'enable-query': 'false',
          'server-ip': '', 'enable-status': 'true'}


def parse(text):
    values = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line or line.startswith('#'):
            continue
        if '=' not in line:
            raise ValueError(f'Configuration line {number}: expected literal key=value')
        key, value = line.split('=', 1)
        if key not in DEFAULTS or key in values:
            raise ValueError(f'Configuration line {number}: unknown or duplicate key')
        values[key] = value  # deliberately no strip, unquote or shell expansion
    missing = DEFAULTS.keys() - values.keys()
    if missing:
        raise ValueError('Missing configuration keys: ' + ', '.join(sorted(missing)))
    return validate(values)


def integer(c, key, low, high):
    value = c[key]
    if not re.fullmatch(r'0|[1-9][0-9]{0,8}', value) or not low <= int(value) <= high:
        raise ValueError(f'{key} must be an integer {low}..{high}')
    return int(value)


def endpoint(value):
    if not value:
        return None
    match = re.fullmatch(r'([A-Za-z0-9][A-Za-z0-9.-]{0,252}):([0-9]{1,5})', value)
    if not match or not 1 <= int(match[2]) <= 65535:
        raise ValueError('public_endpoint must be a hostname or IPv4 followed by :TCP_PORT')
    host = match[1]
    if '..' in host or host.endswith(('.', '-')) or any(part.startswith('-') or len(part) > 63 for part in host.split('.')):
        raise ValueError('Invalid public endpoint hostname')
    if re.fullmatch(r'[0-9.]+', host):
        ipaddress.IPv4Address(host)
    return host, int(match[2])


def validate(c):
    if set(c) != set(DEFAULTS) or any(type(v) is not str for v in c.values()):
        raise ValueError('Configuration keys and string values must match the documented schema')
    for key, value in c.items():
        if len(value) > 512 or any(ord(ch) < 32 or ord(ch) == 127 or 0xD800 <= ord(ch) <= 0xDFFF for ch in value):
            raise ValueError(f'{key} contains unsupported control characters or exceeds 512 characters')
    if c['loader'] not in ('vanilla', 'fabric'):
        raise ValueError('loader must be vanilla or fabric')
    for key in ('minecraft_version', 'fabric_loader_version', 'fabric_installer_version'):
        if c[key] and not VERSION.fullmatch(c[key]):
            raise ValueError(f'{key} must be an exact version, not a URL or path')
    if not c['minecraft_version'] or c['minecraft_version'] in ('latest', 'release'):
        raise ValueError('Select an exact minecraft_version; latest is not a pin')
    for key in ('fabric_loader_version', 'fabric_installer_version'):
        if (c['loader'] == 'fabric') != bool(c[key]):
            raise ValueError('Fabric requires explicit loader AND installer versions; vanilla leaves both blank')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', c['world_name']):
        raise ValueError('world_name must use 1–64 letters, numbers, underscores or hyphens; no path separators')
    if c['world_name'].lower() in ('mods', 'config', 'logs', 'libraries', 'versions', 'cache', 'crash-reports', 'backups'):
        raise ValueError('world_name conflicts with a reserved server directory')
    if c['difficulty'] not in ('peaceful', 'easy', 'normal', 'hard') or c['game_mode'] not in ('survival', 'creative', 'adventure', 'spectator'):
        raise ValueError('Invalid difficulty or game_mode')
    for key in ('whitelist', 'backup_enabled', 'eula'):
        if c[key] not in ('true', 'false'):
            raise ValueError(f'{key} must be true or false')
    for key, lo, hi in [('heap_min_mb', 256, 1048576), ('heap_max_mb', 512, 1048576),
                        ('port', 1024, 65535), ('tunnel_destination_port', 1024, 65535),
                        ('max_players', 1, 1000), ('view_distance', 2, 32),
                        ('simulation_distance', 2, 32), ('backup_retention', 1, 365)]:
        integer(c, key, lo, hi)
    if int(c['heap_min_mb']) > int(c['heap_max_mb']):
        raise ValueError('heap_min_mb must not exceed heap_max_mb')
    if c['port'] != c['tunnel_destination_port']:
        raise ValueError('Tunnel destination port must match the Minecraft listening port. The PUBLIC TCP port may differ.')
    if not re.fullmatch(r'[A-Za-z0-9*,:/ ._-]{1,100}', c['backup_calendar']):
        raise ValueError('backup_calendar must be a single safe systemd OnCalendar expression')
    endpoint(c['public_endpoint'])
    if c['tunnel_destination_ip']:
        address = ipaddress.IPv4Address(c['tunnel_destination_ip'])
        if address.is_loopback or address.is_multicast or address.is_unspecified or address.is_link_local:
            raise ValueError('Shared-agent destination must be a reachable guest IPv4, not loopback/link-local')
    return c


def serialize(c):
    validate(c)
    return '# Literal values: no shell quoting, expansion or inline comments.\n' + ''.join(f'{k}={c[k]}\n' for k in DEFAULTS)


def escape(value):
    # java.util.Properties loads ISO-8859-1; use ASCII and UTF-16 escapes.
    output = []
    for ch in value:
        if ch in '\\=:#! ':
            output.append('\\' + ch)
        elif ord(ch) > 126:
            raw = ch.encode('utf-16-be')
            output.extend('\\u' + raw[i:i+2].hex() for i in range(0, len(raw), 2))
        else:
            output.append(ch)
    return ''.join(output)


def unescape(value):
    def replace(match):
        part = match[1]
        if part.startswith('u'):
            return chr(int(part[1:], 16))
        return {'n': '\n', 'r': '\r', 't': '\t', 'f': '\f'}.get(part, part)
    return re.sub(r'\\(u[0-9a-fA-F]{4}|.)', replace, value)


def logical_lines(text):
    pending = ''
    for physical in text.splitlines(keepends=True):
        body = physical.rstrip('\r\n')
        pending += physical
        if (len(body) - len(body.rstrip('\\'))) % 2:
            continue
        yield pending
        pending = ''
    if pending:
        yield pending


def property_key(line):
    value = line.lstrip(' \t\f')
    if not value or value.startswith(('#', '!')):
        return None
    match = re.match(r'((?:\\.|[^=:\s\\])+)', value)
    return unescape(match[1]) if match else None


def properties(original, c):
    controlled = {k: c[v] for k, v in MANAGED.items()} | FORCED
    # Removing all instances and continuation bodies prevents duplicate/escaped
    # keys from overriding managed settings while preserving unrelated text.
    kept = [line for line in logical_lines(original) if property_key(line) not in controlled and line.strip() != '# Managed by minecraft config apply']
    text = ''.join(kept)
    if text and not text.endswith('\n'):
        text += '\n'
    return text + '# Managed by minecraft config apply\n' + ''.join(f'{k}={escape(v)}\n' for k, v in controlled.items())


def world_guard(data, old, new):
    if old and old['world_name'] != new['world_name']:
        raise ValueError('Changing world_name would select/generate a different world. This release deliberately provides no world reset or switch command.')
    present = (Path(data) / new['world_name']).exists()
    if present and (not old or old['seed'] != new['seed']):
        print('WARNING: An existing world is present. level-seed does not change its terrain or regenerate it.')
