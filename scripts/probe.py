#!/usr/bin/env python3
"""Minecraft Java status protocol probe; safe to copy to the shared agent host."""
import argparse
import json
import socket
import struct
import sys


def varint(number):
    result = bytearray()
    while True:
        byte = number & 127
        number >>= 7
        result.append(byte | (128 if number else 0))
        if not number:
            return bytes(result)


def receive(stream, size):
    output = bytearray()
    while len(output) < size:
        value = stream.recv(size-len(output))
        if not value:
            raise ValueError('Server closed the connection')
        output.extend(value)
    return bytes(output)


def read_varint(stream):
    number = 0
    for shift in range(0, 35, 7):
        byte = receive(stream, 1)[0]
        number |= (byte & 127) << shift
        if byte < 128:
            return number
    raise ValueError('Invalid protocol VarInt')


def status(host, port):
    encoded = host.encode('utf-8')
    if not encoded or len(encoded) > 255 or not 1 <= port <= 65535:
        raise ValueError('Invalid probe endpoint')
    with socket.create_connection((host, port), timeout=5) as stream:
        stream.settimeout(5)
        # Protocol -1 requests status/version discovery, not authentication/login.
        handshake = b'\x00\xff\xff\xff\xff\x0f'+varint(len(encoded))+encoded+struct.pack('>H', port)+b'\x01'
        stream.sendall(varint(len(handshake))+handshake+b'\x01\x00')
        size = read_varint(stream)
        if not 3 <= size <= 1024*1024 or read_varint(stream) != 0:
            raise ValueError('Not a supported Minecraft status response')
        length = read_varint(stream)
        if length > size or length > 1024*1024:
            raise ValueError('Oversized status response')
        response = json.loads(receive(stream, length))
        if not isinstance(response.get('version'), dict) or not isinstance(response.get('players'), dict):
            raise ValueError('Invalid Minecraft status JSON')
        return {'version': response['version'].get('name'), 'online': response['players'].get('online'),
                'max_players': response['players'].get('max')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True)
    parser.add_argument('--port', type=int, default=25565)
    args = parser.parse_args()
    try:
        print(json.dumps({'result': 'PASS', 'endpoint': f'{args.host}:{args.port}', 'status': status(args.host, args.port),
                          'meaning': 'Minecraft status responds from THIS machine. A player login/join has not been tested.'}))
    except (OSError, ValueError) as error:
        print(json.dumps({'result': 'FAIL', 'error': str(error)}))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
