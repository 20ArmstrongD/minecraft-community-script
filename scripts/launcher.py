#!/usr/bin/env python3
"""Unprivileged Java supervisor with a private Unix stdin console, never RCON."""
import json
import os
from pathlib import Path
import selectors
import signal
import socket
import subprocess
import sys

CONF = Path('/etc/minecraft-server')
DATA = Path('/var/lib/minecraft-server')
SOCKET = Path('/run/minecraft-server/console.sock')


def command(text):
    if not text or len(text.encode('utf-8')) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise ValueError('Console command must be a single nonempty line, at most 4096 bytes')
    # Maintenance uses a stopped server, never persistent save-off coordination.
    text = text.strip().removeprefix('/')
    if not text or text.split(' ', 1)[0].lower() in ('save-off', 'stop'):
        raise ValueError('Use minecraft stop for shutdown; save-off is disabled to protect backups')
    return text.removeprefix('/')


def main():
    if any((CONF/name).exists() for name in ('maintenance', 'installing', 'restore-required')):
        raise ValueError('Maintenance/partial installation blocks startup; inspect minecraft info and recovery documentation')
    state = json.loads((CONF/'state.json').read_text())
    launch = state['applied']
    if launch['eula'] != 'true':
        raise ValueError('Minecraft EULA acceptance is required')
    runtime = state['runtime']
    args = [runtime['java'], f'-Xms{launch["heap_min_mb"]}M', f'-Xmx{launch["heap_max_mb"]}M']
    if launch['loader'] == 'fabric':
        args += ['-Dfabric.gameJarPath='+str(Path(runtime['release'])/'server.jar')]
    args += ['-jar', runtime['jar'], 'nogui']
    SOCKET.unlink(missing_ok=True)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(SOCKET)); os.chmod(SOCKET, 0o600); listener.listen(4)
    listener.setblocking(False)
    selector = selectors.DefaultSelector(); selector.register(listener, selectors.EVENT_READ)
    stopping = False
    def terminate(signum, frame):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    # stdout/stderr inherit bounded systemd journal handling; command contents
    # are never printed by this wrapper. No shell or remote command execution.
    process = subprocess.Popen(args, cwd=DATA, stdin=subprocess.PIPE, text=True, encoding='utf-8')
    sent_stop = False
    try:
        while process.poll() is None:
            if stopping and not sent_stop:
                try:
                    process.stdin.write('stop\n'); process.stdin.flush()
                except BrokenPipeError:
                    pass
                sent_stop = True
            for key, events in selector.select(timeout=0.5):
                connection, _ = listener.accept()
                with connection:
                    connection.settimeout(2)
                    raw = bytearray()
                    try:
                        while b'\n' not in raw:
                            chunk = connection.recv(4097-len(raw))
                            if not chunk or len(raw) + len(chunk) > 4096:
                                raise ValueError('Incomplete/oversized console command')
                            raw.extend(chunk)
                        value = command(bytes(raw).decode('utf-8').removesuffix('\n'))
                        if stopping:
                            raise ValueError('Shutdown already in progress')
                        process.stdin.write(value+'\n'); process.stdin.flush()
                        connection.sendall(b'Accepted. Read minecraft logs for the game response.\n')
                    except (ValueError, UnicodeError, OSError):
                        try:
                            connection.sendall(b'Rejected: invalid command, unavailable console or shutdown in progress.\n')
                        except OSError:
                            pass
        code = process.wait()
        # A clean unsolicited Java exit is still an unexpected service failure.
        return 0 if stopping and code == 0 else code or 1
    finally:
        selector.close(); listener.close(); SOCKET.unlink(missing_ok=True)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, OSError, KeyError) as error:
        print('Minecraft launcher: '+str(error), file=sys.stderr)
        sys.exit(1)
