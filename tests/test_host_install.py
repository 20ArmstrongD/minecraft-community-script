import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import host
import install
import manage as m
from test_config import config
import test_management as operations
from test_management import result


class HostTests(unittest.TestCase):
    def args(self,*extra):
        return host.parser().parse_args(['--template','local:vztmpl/debian-13-standard_test_amd64.tar.zst','--ctid','999',*extra])

    def test_unprivileged_without_nesting_or_privileged_features(self):
        args=host.create_args(self.args())
        self.assertEqual(args[args.index('--unprivileged')+1],'1')
        self.assertNotIn('--features',args)
        self.assertIn('firewall=1',args[args.index('--net0')+1])
        self.assertNotIn('docker',str(args))

    def test_static_vlans_dns_overrides(self):
        args=host.create_args(self.args('--ipv4','192.0.2.20/24','--gateway','192.0.2.1','--vlan','25','--dns','192.0.2.1'))
        self.assertIn('gw=192.0.2.1,tag=25',args[args.index('--net0')+1])
        self.assertEqual(args[args.index('--nameserver')+1],'192.0.2.1')

    def test_bad_ctid_network_and_tokens(self):
        for flags in (['--ctid','1'],['--hostname','x;evil'],['--ram','1'],['--cores','0'],['--disk','0'],['--ipv4','192.0.2.20/24','--gateway','198.51.100.1']):
            with self.subTest(flags=flags),self.assertRaises(ValueError):host.validate(self.args(*flags))

    def test_existing_guest_never_created_or_stopped(self):
        with tempfile.TemporaryDirectory() as name:
            path=Path(name)/'config';path.write_text(__import__('configuration').serialize(config()))
            calls=[]
            def run(args,check=True):
                calls.append(args)
                if args[0]=='pveversion':return result('pve-manager/9.0.3')
                return result()
            with patch.object(host.os,'geteuid',return_value=0),patch.object(host.platform,'machine',return_value='x86_64'),patch.object(host,'run',side_effect=run),self.assertRaisesRegex(ValueError,'already exists'):
                host.main(['--template',self.args().template,'--ctid','999','--config',str(path),'--accept-eula','--yes'])
            self.assertFalse(any('create' in call or 'stop' in call for call in calls))

    def test_eula_config_flag_cannot_be_omitted(self):
        with tempfile.TemporaryDirectory() as name:
            path=Path(name)/'config';path.write_text(__import__('configuration').serialize(config()))
            with patch.object(install,'platform_check'),patch.object(install,'install') as execute,self.assertRaisesRegex(ValueError,'explicitly pass --accept-eula'):
                install.main(['--config',str(path),'--yes'])
            execute.assert_not_called()

    def test_unattended_without_config_refused(self):
        with patch.object(install,'platform_check'),self.assertRaises(ValueError):install.main(['--yes','--accept-eula'])

    def test_help_for_all_commands(self):
        for argv in (['--help'],['config','--help'],['update','--help'],['network','configure','--help']):
            with contextlib.redirect_stdout(io.StringIO()),self.assertRaises(SystemExit) as error:m.parser().parse_args(argv)
            self.assertEqual(error.exception.code,0)


class GuestInstallTests(unittest.TestCase):
    setUp = operations.Operations.setUp
    # Inherit the temporary guest/mocked systemd fixture, not its test methods.
    def test_clean_install_has_seed_properties_before_first_start(self):
        import shutil
        for path in (m.ETC,m.DATA,m.RELEASES,m.RUNTIMES):
            if path.exists():shutil.rmtree(path)
        m.ETC.mkdir();m.DATA.mkdir()
        # releases and runtimes share a conceptual /opt parent in production;
        # use independent empty fixture roots here.
        m.RELEASES.mkdir(parents=True);m.RUNTIMES.mkdir(parents=True,exist_ok=True)
        proposed=dict(self.c,seed='literal = world # seed')
        identity=type('Identity',(),{'pw_dir':str(m.DATA),'pw_shell':'/usr/sbin/nologin','pw_uid':123})()
        args=install.parser().parse_args(['--yes','--accept-eula'])
        def first_start(c):
            self.assertFalse((m.ETC/'installing').exists())
            self.assertIn(r'level-seed=literal\ \=\ world\ \#\ seed',(m.DATA/'server.properties').read_text())
            self.assertFalse((m.DATA/'world').exists())
        with patch.object(install,'guard'),patch.object(install.pwd,'getpwnam',return_value=identity),patch.object(install.artifacts,'stage_release',return_value=self.runtime),patch.object(install,'install_code'),patch.object(m,'start',side_effect=first_start) as start:
            install.install(proposed,args)
        start.assert_called_once()
        self.assertEqual(m.state()['applied'],proposed)
