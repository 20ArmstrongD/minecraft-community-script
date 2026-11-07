import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import manage as m
import install as setup
import configuration as cfg
from test_config import config
REAL_RUN = m.run


def result(text='', code=0):
    return subprocess.CompletedProcess([], code, text, '')


class Operations(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        for key in ('ETC', 'DATA', 'BACK', 'RELEASES', 'RUNTIMES', 'UNITS'):
            value=self.root/key; value.mkdir()
            mock=patch.object(m,key,value); mock.start(); self.addCleanup(mock.stop)
        mock=patch.object(m,'LOCK',self.root/'run/lock');mock.start();self.addCleanup(mock.stop)
        for name, value in [('game_identity', (os.getuid(),os.getgid())), ('resources', None), ('port_free', None)]:
            mock=patch.object(m,name,return_value=value);mock.start();self.addCleanup(mock.stop)
        mock=patch.object(m,'private_file', side_effect=lambda p:p);mock.start();self.addCleanup(mock.stop)
        mock=patch.object(m.os, 'chown');mock.start();self.addCleanup(mock.stop)
        self.running=False; self.calls=[]
        def fake_run(args, check=True):
            self.calls.append(args)
            if args[:2] == ['systemctl', 'is-active']: return result(code=0 if self.running else 3)
            if args[:2] == ['systemctl', 'stop']: self.running=False
            if args[:2] == ['systemctl', 'start']: self.running=True
            if args[:2] == ['systemctl', 'show']: return result('MainPID=0\nActiveState=inactive\nControlGroup=\n')
            if args[:3] == ['ip','-j','-4']: return result(json.dumps([{'ifname':'eth0','addr_info':[{'family':'inet','scope':'global','local':'192.0.2.15'}]}]))
            return result()
        mock=patch.object(m,'run',side_effect=fake_run);self.mock_run=mock.start();self.addCleanup(mock.stop)
        self.c=config(); self.runtime={'release':str(m.RELEASES/'old'), 'jar':'/pinned/server.jar','java':'/pinned/bin/java',
             'versions':{k:self.c[k] for k in ('loader','minecraft_version','fabric_loader_version','fabric_installer_version')},
             'provenance':{'java':{'major':21}},'files_sha256':{}}
        m.save_config(self.c);m.save_state({'format':1,'applied':self.c,'runtime':self.runtime})
        (m.DATA/'world').mkdir();(m.DATA/'world/level.dat').write_bytes(b'existing world')
        (m.DATA/'mods').mkdir();(m.DATA/'mods/reviewed.jar').write_bytes(b'reviewed mod')
        (m.DATA/'ops.json').write_text('[]')
        (m.DATA/'server.properties').write_text(cfg.properties('spawn-protection=0\n',self.c))
        output=contextlib.redirect_stdout(io.StringIO());output.__enter__();self.addCleanup(output.__exit__,None,None,None)
        errors=contextlib.redirect_stderr(io.StringIO());errors.__enter__();self.addCleanup(errors.__exit__,None,None,None)

    def test_backup_restore_roundtrip_preserves_world_mods_and_ops(self):
        self.running=True
        m.backup(self.c)
        self.assertTrue(self.running)
        archive=next(m.BACK.glob('*.tar.gz'));m.check_archive(archive)
        (m.DATA/'world/level.dat').write_bytes(b'changed')
        (m.DATA/'mods/reviewed.jar').unlink()
        m.restore(self.c,argparse.Namespace(archive=archive,yes=True))
        self.assertEqual((m.DATA/'world/level.dat').read_bytes(),b'existing world')
        self.assertEqual((m.DATA/'mods/reviewed.jar').read_bytes(),b'reviewed mod')
        self.assertTrue((m.DATA/'ops.json').is_file());self.assertFalse(self.running)
        self.assertEqual(len(list(m.BACK.glob('*.tar.gz'))),2)

    def test_stopped_backup_stays_stopped(self):
        m.backup(self.c);self.assertFalse(self.running)

    def test_backup_failure_inhibits_restart(self):
        self.running=True
        with patch.object(m,'snapshot',side_effect=OSError('disk full')), self.assertRaises(OSError):m.backup(self.c)
        self.assertFalse(self.running);self.assertTrue((m.ETC/'maintenance').exists())
        with self.assertRaises(ValueError):m.start(self.c)

    def test_shutdown_failure_prevents_snapshot(self):
        with patch.object(m,'stop',side_effect=RuntimeError('not stopped')), patch.object(m,'snapshot') as backup,self.assertRaises(RuntimeError):m.backup(self.c)
        backup.assert_not_called();self.assertEqual((m.DATA/'world/level.dat').read_bytes(),b'existing world')

    def test_process_remaining_after_systemd_stop_is_not_safe(self):
        with patch.object(m,'run',return_value=result('MainPID=123\nActiveState=failed\nControlGroup=\n')), self.assertRaises(RuntimeError):m.stop()

    def test_backup_refuses_symlinks_and_preserves_target(self):
        target=self.root/'outside';target.write_bytes(b'private')
        (m.DATA/'link').symlink_to(target)
        with self.assertRaises(ValueError):m.backup(self.c)
        self.assertEqual(target.read_bytes(),b'private')

    def test_corrupt_checksum_rejected_before_any_stop(self):
        archive=m.snapshot(self.c);archive.write_bytes(b'broken')
        with patch.object(m,'stop') as stop,self.assertRaises(ValueError):m.restore(self.c,argparse.Namespace(archive=archive,yes=True))
        stop.assert_not_called()

    def test_traversal_and_link_backups_rejected(self):
        for name,kind in [('../outside',tarfile.REGTYPE),('data/link',tarfile.SYMTYPE)]:
            path=m.BACK/'evil.tar.gz'
            with tarfile.open(path,'w:gz') as tar:
                member=tarfile.TarInfo(name);member.type=kind;member.linkname='/tmp/outside';tar.addfile(member)
            m.atomic(path.with_name(path.name+'.sha256'),m.checksum(path),0o600)
            with self.assertRaises(ValueError):m.check_archive(path)

    def test_no_retention_pruning_for_pre_change_backups(self):
        c=dict(self.c,backup_retention='1')
        m.snapshot(c,prune=False);m.snapshot(c,prune=False)
        self.assertEqual(len(list(m.BACK.glob('*.tar.gz'))),2)
        m.snapshot(c)
        self.assertEqual(len(list(m.BACK.glob('*.tar.gz'))),1)

    def test_backup_uses_applied_config_and_retains_pending_choices(self):
        pending=dict(self.c,seed='future seed');m.save_config(pending)
        archive=m.snapshot(pending)
        with tarfile.open(archive) as tar:
            self.assertEqual(cfg.parse(tar.extractfile('config.env').read().decode()),self.c)
            self.assertEqual(cfg.parse(tar.extractfile('desired.env').read().decode()),pending)

    def test_restore_requires_matching_runtime_pins(self):
        archive=m.snapshot(self.c)
        current=m.state();current['runtime']['versions']['minecraft_version']='1.21.4';m.save_state(current)
        with patch.object(m,'stop') as stop,self.assertRaisesRegex(ValueError,'version pins'):m.restore(self.c,argparse.Namespace(archive=archive,yes=True))
        stop.assert_not_called()

    def test_failed_restore_rolls_back_world_and_config(self):
        archive=m.snapshot(self.c);(m.DATA/'world/level.dat').write_bytes(b'newer world')
        before=(m.ETC/'state.json').read_bytes()
        with patch.object(m,'apply_files',side_effect=OSError('failure')), self.assertRaises(OSError):m.restore(self.c,argparse.Namespace(archive=archive,yes=True))
        self.assertEqual((m.DATA/'world/level.dat').read_bytes(),b'newer world')
        self.assertEqual((m.ETC/'state.json').read_bytes(),before)
        self.assertTrue((m.ETC/'maintenance').exists())

    def test_config_apply_idempotent_without_restart(self):
        self.running=True
        m.config_apply(self.c)
        self.assertFalse(any(call[:2]==['systemctl','stop'] for call in self.calls))
        self.assertFalse(list(m.BACK.glob('*.tar.gz')))

    def test_seed_apply_preserves_existing_world_and_unrelated_properties(self):
        new=dict(self.c,seed='a new text seed',motd='Friends = welcome!')
        m.save_config(new);m.config_apply(new)
        self.assertEqual((m.DATA/'world/level.dat').read_bytes(),b'existing world')
        text=(m.DATA/'server.properties').read_text()
        self.assertIn('spawn-protection=0',text);self.assertIn(r'level-seed=a\ new\ text\ seed',text)
        self.assertEqual(m.state()['applied'],new)

    def test_config_apply_refuses_version_change_or_world_switch(self):
        for new in (dict(self.c,minecraft_version='1.21.4'),dict(self.c,world_name='new')):
            with self.assertRaises(ValueError):m.config_apply(new)
        self.assertEqual((m.DATA/'world/level.dat').read_bytes(),b'existing world')

    def test_failed_config_apply_preserves_exact_prior_properties(self):
        before=(m.DATA/'server.properties').read_bytes()
        with patch.object(m,'apply_files',side_effect=OSError('failed')),self.assertRaises(OSError):m.config_apply(dict(self.c,motd='new'))
        self.assertEqual((m.DATA/'server.properties').read_bytes(),before)
        self.assertTrue((m.ETC/'maintenance').exists())

    def test_existing_installation_rerun_refused(self):
        with self.assertRaisesRegex(ValueError,'Existing server'):setup.guard(self.c)
        self.assertEqual((m.DATA/'world/level.dat').read_bytes(),b'existing world')

    def test_partial_install_requires_explicit_matching_resume(self):
        marker=m.ETC/'installing';m.atomic(marker,json.dumps({'config_sha256':__import__('hashlib').sha256(cfg.serialize(self.c).encode()).hexdigest()}))
        with self.assertRaisesRegex(ValueError,'Partial'):setup.guard(self.c)
        setup.guard(self.c,resume=True)
        with self.assertRaisesRegex(ValueError,'differs'):setup.guard(dict(self.c,seed='different'),resume=True)

    def test_repair_requires_managed_completed_install(self):
        setup.guard(None,repair=True)
        (m.ETC/'state.json').unlink()
        with self.assertRaises(ValueError):setup.guard(None,repair=True)

    def test_update_backups_before_activation_preserves_world(self):
        target=dict(self.runtime,versions=dict(self.runtime['versions'],minecraft_version='1.21.4'))
        args=m.parser().parse_args(['update','--minecraft-version','1.21.4','--yes','--no-start'])
        before=[]
        original=m.apply_files
        def activation(c,runtime):
            before.extend(m.BACK.glob('*.tar.gz'));original(c,runtime)
        with patch.object(m.artifacts,'stage_release',return_value=target),patch.object(m.artifacts,'game_metadata',side_effect=[{'releaseTime':'2024-01-01'},{'releaseTime':'2024-12-01'}]),patch.object(m,'apply_files',side_effect=activation):m.update(self.c,args)
        self.assertTrue(before);self.assertEqual((m.DATA/'world/level.dat').read_bytes(),b'existing world')
        self.assertEqual(m.load()['minecraft_version'],'1.21.4');self.assertFalse(self.running)

    def test_failed_update_keeps_prior_pins_and_world(self):
        args=m.parser().parse_args(['update','--minecraft-version','1.21.4','--yes'])
        with patch.object(m.artifacts,'stage_release',return_value=self.runtime),patch.object(m.artifacts,'game_metadata',side_effect=[{'releaseTime':'2024-01-01'},{'releaseTime':'2024-12-01'}]),patch.object(m,'apply_files',side_effect=OSError('activation failed')),self.assertRaises(OSError):m.update(self.c,args)
        self.assertEqual(m.load(),self.c);self.assertEqual((m.DATA/'world/level.dat').read_bytes(),b'existing world')
        self.assertTrue((m.ETC/'maintenance').exists())

    def test_update_never_implicitly_chooses_latest(self):
        with self.assertRaisesRegex(ValueError,'Explicit version'):m.update(self.c,m.parser().parse_args(['update','--yes']))

    def test_downgrade_needs_restore_and_no_start(self):
        args=m.parser().parse_args(['update','--minecraft-version','1.20.4','--yes','--allow-downgrade','--no-start'])
        target=dict(self.runtime,versions=dict(self.runtime['versions'],minecraft_version='1.20.4'))
        with patch.object(m.artifacts,'stage_release',return_value=target),patch.object(m.artifacts,'game_metadata',side_effect=[{'releaseTime':'2024-01-01'},{'releaseTime':'2023-01-01'}]):m.update(self.c,args)
        self.assertTrue((m.ETC/'restore-required').exists())
        with self.assertRaises(ValueError):m.start(m.load())

    def test_shared_agent_config_does_not_restart_game(self):
        args=m.parser().parse_args(['network','configure','--agent-location','Another LXC on LAN','--destination-ip','192.0.2.15','--public-endpoint','example.gl.joinmc.link:30111','--yes'])
        m.network(self.c,args)
        self.assertEqual(m.load()['public_endpoint'],'example.gl.joinmc.link:30111')
        self.assertFalse(any(call[:2] in (['systemctl','start'],['systemctl','stop']) for call in self.calls))
        self.assertFalse(any('playit' in str(call) for call in self.calls))

    def test_remote_loopback_or_foreign_destination_refused(self):
        for ip in ('127.0.0.1','192.0.2.20'):
            args=m.parser().parse_args(['network','configure','--agent-location','LAN','--destination-ip',ip,'--public-endpoint','example.net:30000','--yes'])
            with self.assertRaises(ValueError):m.network(self.c,args)
        self.assertEqual(m.load(),self.c)

    def test_scheduled_backup_disabled_is_noop(self):
        m.backup(dict(self.c,backup_enabled='false'),True)
        self.assertFalse(list(m.BACK.glob('*.tar.gz')))

    def test_start_requires_apply_and_recovery(self):
        with self.assertRaisesRegex(ValueError,'unapplied'):m.start(dict(self.c,motd='not applied'))

    def test_missing_dependency_has_readable_error(self):
        with patch.object(subprocess,'run',side_effect=FileNotFoundError()),self.assertRaisesRegex(ValueError,'Missing dependency'):REAL_RUN(['missing-command'])

    def test_lock_rejects_symlink_without_changing_target(self):
        target=self.root/'valuable';target.write_bytes(b'private');target.chmod(0o640)
        m.LOCK.parent.mkdir(parents=True);m.LOCK.symlink_to(target)
        with self.assertRaises(OSError):
            with m.lock():pass
        self.assertEqual(target.read_bytes(),b'private')
        self.assertEqual(target.stat().st_mode&0o777,0o640)

    def test_installer_rejects_symlinked_game_directory(self):
        import shutil
        shutil.rmtree(m.DATA)
        target=self.root/'unrelated';target.mkdir();m.DATA.symlink_to(target)
        with self.assertRaisesRegex(ValueError,'symbolic links'):setup.guard(self.c)
        self.assertEqual(list(target.iterdir()),[])

    def test_network_failure_cannot_change_game_config(self):
        before=(m.ETC/'config.env').read_bytes()
        args=m.parser().parse_args(['network','test'])
        with patch.object(m.probe,'status',side_effect=OSError('unreachable')),self.assertRaises(OSError):m.network(self.c,args)
        self.assertEqual((m.ETC/'config.env').read_bytes(),before)

    def test_stop_and_logs_work_even_with_broken_config(self):
        with patch.object(m.os,'geteuid',return_value=0),patch.object(m,'lock',side_effect=contextlib.nullcontext),patch.object(m,'load',side_effect=ValueError('bad config')),patch.object(m,'stop') as stopped:
            self.assertEqual(m.main(['stop']),0)
        stopped.assert_called_once()
        with patch.object(m.os,'geteuid',return_value=0),patch.object(m,'load',side_effect=ValueError('bad config')),patch.object(m.os,'execvp',side_effect=RuntimeError('exec')) as execute,self.assertRaisesRegex(RuntimeError,'exec'):
            m.main(['logs','--no-follow'])
        execute.assert_called_once()
