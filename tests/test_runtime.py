import io
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import launcher
import probe


class FakeSocket:
    def __init__(self, response):self.response=io.BytesIO(response);self.sent=b''
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def settimeout(self,value):pass
    def sendall(self,data):self.sent+=data
    def recv(self,count):return self.response.read(count)


class ProtocolTests(unittest.TestCase):
    def test_minecraft_status_request_and_response(self):
        payload=json.dumps({'version':{'name':'Pinned Version'},'players':{'online':2,'max':10}}).encode()
        packet=b'\x00'+probe.varint(len(payload))+payload
        stream=FakeSocket(probe.varint(len(packet))+packet)
        with patch.object(probe.socket,'create_connection',return_value=stream):
            result=probe.status('192.0.2.20',25565)
        self.assertEqual(result,{'version':'Pinned Version','online':2,'max_players':10})
        self.assertTrue(stream.sent.endswith(b'\x01\x00'))
        self.assertNotIn(b'password',stream.sent)

    def test_refuse_http_or_invalid_protocol_response(self):
        for data in (b'HTTP/1.0 200 OK',b'\xff'*6,b'\x04\x00\xff'*4,b''):
            with patch.object(probe.socket,'create_connection',return_value=FakeSocket(data)),self.assertRaises(ValueError):probe.status('example.net',25565)

    def test_console_single_line_and_save_protection(self):
        self.assertEqual(launcher.command('/whitelist add Player'), 'whitelist add Player')
        self.assertEqual(launcher.command('say Hello friends!'), 'say Hello friends!')
        for command in ('stop','/stop','save-off','say hi\nstop','', 'x'*4097):
            with self.assertRaises(ValueError):launcher.command(command)


class SupervisorTests(unittest.TestCase):
    def execute(self, game_source, interact=None):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);conf=root/'etc';conf.mkdir();data=root/'data';data.mkdir();socket_path=root/'console.sock'
            java=root/'fake-java';java.write_text('#!'+sys.executable+'\n'+game_source);java.chmod(0o755)
            (conf/'state.json').write_text(json.dumps({'applied':{'eula':'true','loader':'vanilla','heap_min_mb':'512','heap_max_mb':'1024'},'runtime':{'java':str(java),'jar':'/pinned.jar'}}))
            runner=root/'runner.py'
            source=str(Path(launcher.__file__).resolve().parent)
            runner.write_text(f'import sys\nsys.path.insert(0,{source!r})\nimport launcher\nfrom pathlib import Path\nlauncher.CONF=Path({str(conf)!r})\nlauncher.DATA=Path({str(data)!r})\nlauncher.SOCKET=Path({str(socket_path)!r})\nsys.exit(launcher.main())\n')
            if interact:
                try:
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as check:
                        check.bind(str(root/'permission-check.sock'))
                except PermissionError:
                    self.skipTest('Sandbox prohibits Unix socket bind; real console integration needs Linux CI')
            else:
                # Exercise actual child exit/wait code without requiring socket privileges.
                runner.write_text(runner.read_text().replace('sys.exit(launcher.main())',
                    'from unittest.mock import patch\nwith patch("launcher.socket.socket"), patch("launcher.selectors.DefaultSelector"), patch("launcher.os.chmod"):\n sys.exit(launcher.main())'))
            process=subprocess.Popen([sys.executable,str(runner)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            try:
                if interact:
                    interact(process,socket_path)
                out,err=process.communicate(timeout=8)
                return process.returncode,out,err
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:process.wait(timeout=3)
                    except subprocess.TimeoutExpired:process.kill();process.wait()
                process.stdout.close();process.stderr.close()

    def test_zero_exit_is_an_unexpected_service_failure(self):
        code,_,err=self.execute('import sys\nsys.exit(0)\n')
        self.assertNotIn('Traceback',err)
        self.assertEqual(code,1,err)

    def test_nonzero_exit_preserved(self):
        code,_,err=self.execute('import sys\nsys.exit(23)\n')
        self.assertEqual(code,23,err)

    def test_secure_console_and_graceful_stop(self):
        def interact(process,path):
            deadline=time.monotonic()+4
            while not path.exists() and time.monotonic()<deadline:
                if process.poll() is not None:break
                time.sleep(0.02)
            self.assertTrue(path.exists())
            self.assertEqual(path.stat().st_mode&0o777,0o600)
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as stream:
                stream.settimeout(3);stream.connect(str(path));stream.sendall(b'say Hello friends\n')
                self.assertIn(b'Accepted',stream.recv(512))
            process.send_signal(signal.SIGTERM)
        code,out,err=self.execute('import sys\nfor line in sys.stdin:\n print("GAME_COMMAND="+line.strip(),flush=True)\n if line.strip()=="stop":break\n',interact)
        self.assertEqual(code,0,err)
        self.assertIn('GAME_COMMAND=say Hello friends',out)
        self.assertIn('GAME_COMMAND=stop',out)
