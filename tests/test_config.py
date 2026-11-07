import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import configuration as c


def config(**values):
    return dict(c.DEFAULTS, minecraft_version='1.21.1', eula='true') | values


class ConfigTests(unittest.TestCase):
    def test_roundtrip_literal_values_and_empty_seed(self):
        values = config(seed='  a = b # $(touch /tmp/not-executed) `cmd` "x" \\ ü 🌍  ', motd='Hello: Friends!')
        self.assertEqual(c.parse(c.serialize(values)), values)
        self.assertEqual(c.parse(c.serialize(config()))['seed'], '')

    def test_reject_unknown_duplicate_missing_and_shell_syntax(self):
        for text in (c.serialize(config())+'port=25565\n', c.serialize(config())+'unknown=yes\n', 'source=/tmp/code\n', 'export port=25565\n', 'port=25565\n'):
            with self.subTest(text=text[-25:]), self.assertRaises(ValueError): c.parse(text)

    def test_validation_boundaries(self):
        for key, value in [('port', '0'), ('port', '65536'), ('max_players', '-1'), ('world_name', '../world'),
                           ('world_name', 'mods'), ('motd', 'hello\nworld'), ('seed', 'x\x00'), ('loader', 'paper'),
                           ('heap_min_mb', '999999'), ('whitelist', 'yes'), ('minecraft_version', 'latest'),
                           ('public_endpoint', 'x:70000'), ('public_endpoint', '999.1.1.1:25565'),
                           ('tunnel_destination_ip', '127.0.0.1'), ('backup_calendar', 'daily\nExecStart=code')]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                c.validate(config(**{key: value}))

    def test_no_silent_fabric_pin_defaults(self):
        with self.assertRaises(ValueError): c.validate(config(loader='fabric'))
        self.assertEqual(c.validate(config(loader='fabric', fabric_loader_version='0.16.10', fabric_installer_version='1.0.1'))['loader'], 'fabric')

    def test_property_escape_and_unrelated_continuations_preserved(self):
        original = '# admin settings\nspawn-protection=0\ncustom=hello\\\n  there\nlevel-seed=wrong\nserver\\-port:1\nserver-port=2\n'
        output = c.properties(original, config(seed=' abc:=#!\\'))
        self.assertIn('spawn-protection=0\ncustom=hello\\\n  there\n', output)
        self.assertIn('level-seed=\\ abc\\:\\=\\#\\!\\\\\n', output)
        self.assertEqual(output.count('server-port='), 1)
        self.assertNotIn('server\\-port:', output)
        self.assertIn('online-mode=true\n', output)
        self.assertIn('enable-rcon=false\n', output)
        self.assertEqual(c.properties(output, config(seed=' abc:=#!\\')), output)

    def test_unicode_converts_to_java_utf16(self):
        self.assertEqual(c.escape('🌍é'), r'\ud83c\udf0d\u00e9')

    def test_managed_continuation_body_cannot_override(self):
        result = c.properties('motd=old\\\nworld\nother=value\n', config())
        self.assertNotIn('\nworld\n', result)
        self.assertIn('other=value\n', result)

    def test_existing_world_is_never_reset_by_seed(self):
        with tempfile.TemporaryDirectory() as path:
            root=Path(path); (root/'world').mkdir(); marker=root/'world/level.dat'; marker.write_bytes(b'existing')
            with contextlib.redirect_stdout(io.StringIO()) as output:
                c.world_guard(root, config(), config(seed='new seed'))
            self.assertIn('does not change', output.getvalue())
            self.assertEqual(marker.read_bytes(), b'existing')
            with self.assertRaises(ValueError): c.world_guard(root, config(), config(world_name='other'))

    def test_public_port_translation_is_allowed_destination_must_match(self):
        self.assertEqual(c.validate(config(public_endpoint='example.gl.joinmc.link:32100'))['port'], '25565')
        with self.assertRaises(ValueError): c.validate(config(tunnel_destination_port='25566'))

    def test_example_is_valid_and_not_eula_acceptance(self):
        values=c.parse((Path(__file__).resolve().parents[1]/'config/example.env').read_text())
        self.assertEqual(values['eula'], 'false')
