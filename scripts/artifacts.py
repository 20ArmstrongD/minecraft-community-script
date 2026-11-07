"""Official metadata resolution, bounded HTTPS transfers and verified staging."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import tarfile
import urllib.parse
import urllib.request
import uuid

MANIFEST = 'https://piston-meta.mojang.com/mc/game/version_manifest_v2.json'
FABRIC = 'https://meta.fabricmc.net/v2/versions'
ALLOWED = {'piston-meta.mojang.com', 'piston-data.mojang.com', 'launchermeta.mojang.com',
           'launcher.mojang.com', 'meta.fabricmc.net', 'maven.fabricmc.net', 'libraries.minecraft.net',
           'api.adoptium.net', 'github.com', 'release-assets.githubusercontent.com',
           'objects.githubusercontent.com', 'github-releases.githubusercontent.com'}


def url_check(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname not in ALLOWED or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError('Download URL must use HTTPS on an approved official distribution host')


class Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        url_check(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url, target=None, algorithm=None, expected=None, limit=8*1024*1024, size=None):
    url_check(url)
    if expected is not None and (algorithm not in ('sha1', 'sha256', 'sha512') or not re.fullmatch(r'[0-9a-fA-F]{'+str(hashlib.new(algorithm).digest_size*2)+'}', expected)):
        raise ValueError('Missing/invalid publisher checksum')
    request = urllib.request.Request(url, headers={'User-Agent': 'Minecraft-Community-Script/0.1'})
    digest = hashlib.new(algorithm or 'sha256')
    output = bytearray()
    temporary = target.with_name(target.name+'.part') if target else None
    try:
        if target:
            target.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.build_opener(Redirect()).open(request, timeout=60) as response:
            url_check(response.url)
            stream = temporary.open('wb') if temporary else None
            total = 0
            try:
                while chunk := response.read(128*1024):
                    total += len(chunk)
                    if total > limit:
                        raise ValueError('Download exceeds size limit')
                    digest.update(chunk)
                    if stream:
                        stream.write(chunk)
                    else:
                        output.extend(chunk)
            finally:
                if stream:
                    stream.close()
        if size is not None and total != size or expected is not None and digest.hexdigest() != expected.lower():
            raise ValueError('Downloaded artifact checksum/size mismatch; no activation performed')
        if target:
            os.replace(temporary, target)
            return digest.hexdigest()
        return bytes(output)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def document(url):
    return json.loads(fetch(url))


def game_metadata(version):
    manifest = document(MANIFEST)
    matches = [v for v in manifest['versions'] if v['id'] == version and v['type'] == 'release']
    if len(matches) != 1:
        raise ValueError('Select an exact official Minecraft release; snapshots and unknown versions are unsupported')
    entry = matches[0]
    raw = fetch(entry['url'], algorithm='sha1', expected=entry['sha1'])
    data = json.loads(raw)
    major = data.get('javaVersion', {}).get('majorVersion')
    if data.get('id') != version or major not in (17, 21, 25):
        raise ValueError('Version lacks supported official Java metadata (supported majors: 17, 21, 25); refusing to guess')
    return data


def safe_runtime_extract(archive, dest):
    with tarfile.open(archive, 'r:gz') as tar:
        members = tar.getmembers()
        total = 0
        for member in members:
            parts = PurePosixPath(member.name)
            if parts.is_absolute() or '..' in parts.parts or not parts.parts or member.isdev() or member.isfifo() or member.islnk():
                raise ValueError('Unsafe Java archive member')
            total += member.size
            if total > 1024**3 or len(members) > 50000:
                raise ValueError('Java archive exceeds extraction limits')
            if member.issym():
                resolved = (dest / member.name).parent.joinpath(member.linkname).resolve()
                if not resolved.is_relative_to(dest.resolve()):
                    raise ValueError('Java archive symlink escapes staging')
        # Only checked JRE files, directories and internal symlinks; Python 3.11 compatible.
        for member in members:
            if not (member.isfile() or member.isdir() or member.issym()):
                raise ValueError('Unsupported Java archive member')
        tar.extractall(dest, members=members, filter='data')


def java_runtime(major, runtime_root):
    arch = {'x86_64': 'x64', 'aarch64': 'aarch64'}.get(platform.machine())
    if not arch:
        raise ValueError('Only amd64/arm64 Linux are supported')
    record = document(f'https://api.adoptium.net/v3/assets/latest/{major}/hotspot?architecture={arch}&image_type=jre&os=linux&vendor=eclipse')
    if not isinstance(record, list) or not record:
        raise ValueError('No supported Temurin JRE available for the required Java version')
    record = record[0]; package = record['binary']['package']
    if record['version']['major'] != major or record['binary']['architecture'] != arch or record['binary']['os'] != 'linux':
        raise ValueError('Java provider metadata mismatch')
    digest = package['checksum']
    if not re.fullmatch('[a-f0-9]{64}', digest):
        raise ValueError('Invalid Java SHA-256')
    final = runtime_root / digest
    if not final.exists():
        stage = runtime_root / ('.stage-'+uuid.uuid4().hex)
        stage.mkdir(parents=True)
        try:
            archive = stage/'java.tar.gz'
            fetch(package['link'], archive, 'sha256', digest, limit=512*1024**2, size=package['size'])
            safe_runtime_extract(archive, stage/'unpack')
            roots = list((stage/'unpack').iterdir())
            if len(roots) != 1 or not (roots[0]/'bin/java').is_file():
                raise ValueError('Unexpected JRE archive layout')
            os.replace(roots[0], final)
        finally:
            shutil.rmtree(stage)
    java = final/'bin/java'
    result = subprocess.run([str(java), '-version'], capture_output=True, text=True, check=True)
    if not re.search(r'version "'+str(major)+r'(?:\.|\")', result.stderr + result.stdout):
        raise ValueError('Downloaded Java reports the wrong major version')
    return str(java), {'major': major, 'release': record['release_name'], 'sha256': digest, 'url': package['link']}


def stage_release(c, releases, runtime_root, user='minecraft'):
    meta = game_metadata(c['minecraft_version'])
    java, runtime = java_runtime(meta['javaVersion']['majorVersion'], runtime_root)
    releases.mkdir(parents=True, exist_ok=True)
    stage = releases / ('.stage-'+uuid.uuid4().hex)
    stage.mkdir()
    try:
        info = meta['downloads']['server']
        fetch(info['url'], stage/'server.jar', 'sha1', info['sha1'], limit=512*1024**2, size=info['size'])
        jar = 'server.jar'
        provenance = {'minecraft': {'version': c['minecraft_version'], 'url': info['url'], 'sha1': info['sha1']}, 'java': runtime}
        if c['loader'] == 'fabric':
            available = document(f'{FABRIC}/loader/{c["minecraft_version"]}/{c["fabric_loader_version"]}')
            if available['loader']['version'] != c['fabric_loader_version']:
                raise ValueError('Fabric metadata does not match the pinned loader')
            version = c['fabric_installer_version']
            installer_url = f'https://maven.fabricmc.net/net/fabricmc/fabric-installer/{version}/fabric-installer-{version}.jar'
            checksum = fetch(installer_url+'.sha256').decode('ascii').strip()
            fetch(installer_url, stage/'installer.jar', 'sha256', checksum, limit=32*1024**2)
            # Only official verified code runs, under the unprivileged game account.
            import pwd
            identity = pwd.getpwnam(user)
            os.chown(stage, identity.pw_uid, identity.pw_gid)
            subprocess.run(['runuser', '-u', user, '--', java, '-jar', str(stage/'installer.jar'), 'server',
                            '-dir', str(stage), '-mcversion', c['minecraft_version'], '-loader', c['fabric_loader_version']], check=True, timeout=600, cwd=stage)
            # Validate every fetched Maven library independently after the installer.
            profile = document(f'{FABRIC}/loader/{c["minecraft_version"]}/{c["fabric_loader_version"]}/server/json')
            expected_paths = set()
            for library in profile['libraries']:
                parts = library['name'].split(':')
                if len(parts) != 3 or not re.fullmatch(r'[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*', parts[0]) or any(not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.+-]*', v) for v in parts[1:]):
                    raise ValueError('Unsupported Fabric library coordinate')
                group, name, ver = parts
                path = f'{group.replace(".", "/")}/{name}/{ver}/{name}-{ver}.jar'
                expected_paths.add(path)
                target = stage/'libraries'/path
                url = library['url'].rstrip('/')+'/'+path
                expected = fetch(url+'.sha1').decode('ascii').strip()
                if not re.fullmatch('[a-fA-F0-9]{40}', expected) or not target.is_file() or hashlib.sha1(target.read_bytes()).hexdigest() != expected.lower():
                    raise ValueError('Fabric library checksum mismatch')
            actual = {str(p.relative_to(stage/'libraries')) for p in (stage/'libraries').rglob('*.jar')}
            if actual != expected_paths:
                raise ValueError('Unexpected Fabric dependency set')
            jar = 'fabric-server-launch.jar'
            if not (stage/jar).is_file():
                raise ValueError('Fabric installer produced no launcher')
            provenance['fabric'] = {'loader': c['fabric_loader_version'], 'installer': version, 'installer_sha256': checksum}
        if hashlib.sha1((stage/'server.jar').read_bytes()).hexdigest() != info['sha1']:
            raise ValueError('Staged Minecraft JAR changed during preparation')
        for path in stage.rglob('*'):
            if path.is_symlink():
                raise ValueError('Unexpected symlink in staged release')
            os.chown(path, 0, 0)
            path.chmod(0o755 if path.is_dir() else 0o644)
        os.chown(stage, 0, 0); stage.chmod(0o755)
        # Record every release file, including the generated launcher.
        hashes = {str(p.relative_to(stage)): hashlib.sha256(p.read_bytes()).hexdigest() for p in stage.rglob('*') if p.is_file()}
        final = releases / uuid.uuid4().hex
        os.replace(stage, final)
        return {'release': str(final), 'jar': str(final/jar), 'java': java,
                'versions': {k: c[k] for k in ('loader', 'minecraft_version', 'fabric_loader_version', 'fabric_installer_version')},
                'provenance': provenance, 'files_sha256': hashes}
    finally:
        if stage.exists():
            shutil.rmtree(stage)
