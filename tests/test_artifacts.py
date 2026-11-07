import hashlib
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
import artifacts as a
from test_config import config


class Response(io.BytesIO):
    url='https://piston-data.mojang.com/test.jar'


class Downloads(unittest.TestCase):
    def test_hash_checked_atomic_download_and_mismatch_cleanup(self):
        with tempfile.TemporaryDirectory() as name:
            target=Path(name)/'server.jar';target.write_bytes(b'original')
            digest=hashlib.sha1(b'official jar').hexdigest()
            with patch.object(a.urllib.request,'build_opener') as opener:
                opener.return_value.open.side_effect=lambda *x,**y:Response(b'official jar')
                a.fetch(Response.url,target,'sha1',digest,size=12)
                self.assertEqual(target.read_bytes(),b'official jar')
                with self.assertRaises(ValueError):a.fetch(Response.url,target,'sha1','0'*40)
                self.assertEqual(target.read_bytes(),b'official jar')
                self.assertFalse(target.with_name('server.jar.part').exists())

    def test_size_and_allocation_bounds(self):
        with patch.object(a.urllib.request,'build_opener') as opener:
            opener.return_value.open.side_effect=lambda *x,**y:Response(b'large')
            for kw in ({'limit':2},{'size':1}):
                with self.assertRaises(ValueError):a.fetch(Response.url,**kw)

    def test_unapproved_and_cleartext_hosts_rejected(self):
        for url in ('http://piston-data.mojang.com/a','https://evil.example/a','https://user:pass@maven.fabricmc.net/a','file:///etc/passwd'):
            with self.subTest(url=url),self.assertRaises(ValueError):a.url_check(url)
        with self.assertRaises(ValueError):a.Redirect().redirect_request(None,None,302,'',{},'http://evil.example')

    def test_metadata_determines_java_without_version_number_guessing(self):
        for version,java in [('1.20.4',17),('1.21.1',21),('26.1',25)]:
            raw=json.dumps({'id':version,'javaVersion':{'majorVersion':java}}).encode()
            manifest={'versions':[{'id':version,'type':'release','url':Response.url,'sha1':hashlib.sha1(raw).hexdigest()}]}
            with patch.object(a,'document',return_value=manifest),patch.object(a,'fetch',return_value=raw) as fetch:
                self.assertEqual(a.game_metadata(version)['javaVersion']['majorVersion'],java)
                self.assertEqual(fetch.call_args.kwargs['expected'],hashlib.sha1(raw).hexdigest())

    def test_unknown_snapshot_or_future_java_fail_closed(self):
        with patch.object(a,'document',return_value={'versions':[]}):
            with self.assertRaises(ValueError):a.game_metadata('latest')
        for payload in ({'id':'1.21.1'}, {'id':'1.21.1','javaVersion':{'majorVersion':99}}):
            with patch.object(a,'document',return_value={'versions':[{'id':'1.21.1','type':'release','url':Response.url,'sha1':'a'*40}]}),patch.object(a,'fetch',return_value=json.dumps(payload).encode()):
                with self.assertRaises(ValueError):a.game_metadata('1.21.1')

    def test_java_archive_rejects_traversal_and_escaping_links(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);archive=root/'java.tar.gz'
            for path,kind,target in [('../outside',tarfile.REGTYPE,''),('java/lib/link',tarfile.SYMTYPE,'../../../outside')]:
                with tarfile.open(archive,'w:gz') as tar:
                    member=tarfile.TarInfo(path);member.type=kind;member.linkname=target;tar.addfile(member)
                with self.assertRaises(ValueError):a.safe_runtime_extract(archive,root/'out')

    def test_valid_java_archive_preserves_executable(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);archive=root/'java.tar.gz'
            with tarfile.open(archive,'w:gz') as tar:
                member=tarfile.TarInfo('jre/bin/java');member.mode=0o755;member.size=4;tar.addfile(member,io.BytesIO(b'java'))
            a.safe_runtime_extract(archive,root/'out')
            self.assertTrue(os.access(root/'out/jre/bin/java',os.X_OK))

    def test_vanilla_staging_does_not_touch_world_and_records_hashes(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);(root/'world').mkdir();(root/'world/level.dat').write_bytes(b'save')
            data=b'official bytes';digest=hashlib.sha1(data).hexdigest()
            metadata={'javaVersion':{'majorVersion':21},'downloads':{'server':{'url':Response.url,'sha1':digest,'size':len(data)}}}
            def fetch(url,target,*args,**kw):target.write_bytes(data)
            with patch.object(a,'game_metadata',return_value=metadata),patch.object(a,'java_runtime',return_value=('/java',{'major':21})),patch.object(a,'fetch',side_effect=fetch),patch.object(a.os,'chown'):
                runtime=a.stage_release(config(),root/'releases',root/'java')
            self.assertEqual(runtime['files_sha256']['server.jar'],hashlib.sha256(data).hexdigest())
            self.assertEqual((root/'world/level.dat').read_bytes(),b'save')
            self.assertFalse(any(p.name.startswith('.stage') for p in (root/'releases').iterdir()))

    def test_failed_staging_cleans_only_temporary_release(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);releases=root/'releases';releases.mkdir();(releases/'old').mkdir();(releases/'old/save').write_bytes(b'keep')
            with patch.object(a,'game_metadata',return_value={'javaVersion':{'majorVersion':21},'downloads':{'server':{'url':Response.url,'sha1':'a'*40,'size':5}}}),patch.object(a,'java_runtime',return_value=('/java',{})),patch.object(a,'fetch',side_effect=ValueError('hash mismatch')),self.assertRaises(ValueError):a.stage_release(config(),releases,root/'java')
            self.assertEqual((releases/'old/save').read_bytes(),b'keep')
            self.assertEqual([p.name for p in releases.iterdir()],['old'])


class FabricStaging(unittest.TestCase):
    def test_pinned_installer_libraries_and_game_jar_verified(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);server=b'verified Minecraft';library=b'verified Fabric library';installer=b'verified installer'
            metadata={'javaVersion':{'majorVersion':21},'downloads':{'server':{'url':Response.url,'sha1':hashlib.sha1(server).hexdigest(),'size':len(server)}}}
            coordinate='net/fabricmc/fabric-loader/0.16.10/fabric-loader-0.16.10.jar'
            def download(url,target=None,*args,**kwargs):
                if url.endswith('.sha256'):return hashlib.sha256(installer).hexdigest().encode()
                if url.endswith('.sha1'):return hashlib.sha1(library).hexdigest().encode()
                target.write_bytes(server if url==Response.url else installer)
            def document(url):
                if url.endswith('/server/json'):return {'libraries':[{'name':'net.fabricmc:fabric-loader:0.16.10','url':'https://maven.fabricmc.net/'}]}
                return {'loader':{'version':'0.16.10'}}
            def run(args,**kwargs):
                self.assertIn('-mcversion',args);self.assertIn('1.21.1',args);self.assertIn('-loader',args);self.assertIn('0.16.10',args)
                self.assertNotIn('-downloadMinecraft',args)
                stage=Path(args[args.index('-dir')+1]);(stage/'fabric-server-launch.jar').write_bytes(b'generated launcher')
                target=stage/'libraries'/coordinate;target.parent.mkdir(parents=True);target.write_bytes(library)
            identity=type('Identity',(),{'pw_uid':os.getuid(),'pw_gid':os.getgid()})()
            with patch.object(a,'game_metadata',return_value=metadata),patch.object(a,'java_runtime',return_value=('/java',{'major':21})),patch.object(a,'fetch',side_effect=download),patch.object(a,'document',side_effect=document),patch.object(a.subprocess,'run',side_effect=run),patch.object(__import__('pwd'),'getpwnam',return_value=identity),patch.object(a.os,'chown'):
                runtime=a.stage_release(config(loader='fabric',fabric_loader_version='0.16.10',fabric_installer_version='1.0.1'),root/'releases',root/'java')
            self.assertTrue(runtime['jar'].endswith('fabric-server-launch.jar'))
            self.assertIn('libraries/'+coordinate,runtime['files_sha256'])
            self.assertEqual(runtime['provenance']['fabric']['installer_sha256'],hashlib.sha256(installer).hexdigest())
