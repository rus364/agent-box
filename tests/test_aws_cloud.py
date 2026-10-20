"""Forwarding must expose only the selected AWS authentication closure, without host writes."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_box import aws_cloud, cloud, kubeconfigs, launch_options, runtime
from agent_box.config import Config

CONFIG = '''[default]
login_session = arn:aws:sts::111:assumed-role/dev/user
region = eu-west-1
[profile role]
role_arn = arn:aws:iam::111:role/test
source_profile = base
services = local
region = eu-west-2
output = json
[services local]
s3 =
  endpoint_url = http://localhost:9000
[profile base]
region = eu-west-1
[profile team]
sso_session = work
sso_account_id = 111
sso_role_name = dev
[sso-session work]
sso_start_url = https://example.awsapps.com/start
sso_region = eu-west-1
sso_registration_scopes = sso:account:access
[profile other]
sso_session = hidden
sso_account_id = 222
sso_role_name = admin
[sso-session hidden]
sso_start_url = https://hidden.awsapps.com/start
sso_region = us-east-1
[profile process]
credential_process = /host/bin/SECRET-PROCESS
[profile mfa]
role_arn = arn:aws:iam::111:role/mfa
source_profile = base
mfa_serial = SECRET-MFA
'''
CREDS = '''[base]
aws_access_key_id = SECRET-KEY
aws_secret_access_key = SECRET-ACCESS
aws_session_token = SECRET-SESSION
[hidden]
aws_access_key_id = SECRET-HIDDEN
aws_secret_access_key = SECRET-HIDDEN-ACCESS
'''


def config(*names, enabled=True):
    c = Config()
    c.credentials.cloud.enabled = enabled
    c.credentials.cloud.aws = list(names)
    return c


class AwsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.host = self.root / 'host-aws'
        self.host.mkdir()
        (self.host / 'config').write_text(CONFIG)
        (self.host / 'credentials').write_text(CREDS)
        for target, key, value in ((aws_cloud, 'AWS_CONFIG_DIR', self.host),
                                   (cloud, 'CLOUD_RUNTIME_DIR', self.root / 'runtime')):
            patcher = mock.patch.object(target, key, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.sso = self.cache('sso', 'work', {'accessToken': 'SECRET-SSO', 'expiresAt': '2099-01-01T00:00:00Z',
                              'startUrl': 'https://example.awsapps.com/start', 'region': 'eu-west-1',
                              'refreshToken': 'SECRET-REFRESH', 'clientSecret': 'SECRET-CLIENT'})
        self.cache('sso', 'hidden', {'accessToken': 'SECRET-HIDDEN'})
        self.login = self.cache('login', 'arn:aws:sts::111:assumed-role/dev/user',
                               {'accessToken': {'accessKeyId': 'SECRET-LOGIN-KEY', 'secretAccessKey': 'SECRET-LOGIN-ACCESS',
                                                'sessionToken': 'SECRET-LOGIN-TOKEN', 'accountId': '111',
                                                'expiresAt': '2099-01-01T00:00:00Z'},
                                'refreshToken': 'SECRET-REFRESH', 'dpopKey': 'SECRET-DPOP', 'clientId': 'client'})

    def cache(self, kind, key, data):
        digest = hashlib.sha1(key.encode()).hexdigest() if kind == 'sso' else hashlib.sha256(key.encode()).hexdigest()
        p = self.host / kind / 'cache' / (digest + '.json')
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data))
        return p

    def box(self, *parts):
        return aws_cloud.runtime_dir('ws').joinpath(*parts)

    def host_bytes(self):
        return {str(p): p.read_bytes() for p in self.host.rglob('*') if p.is_file()}

    def test_old_configs_and_override_do_not_enable_new_account_access(self):
        """Old yc opt-ins must never select an AWS account on upgrade."""
        c = Config.from_dict({'credentials': {'cloud': {'enabled': True, 'yc': {'profiles': ['old']}}}})
        self.assertEqual(c.credentials.cloud.aws, [])
        self.assertFalse(c.box.tools.groups['aws'])
        c = config('base', enabled=False)
        launch_options.apply(c, {'cloud-profiles': True})
        self.assertTrue(c.credentials.cloud.active)
        self.assertEqual(c.credentials.needed_tools(), {'aws': 'Cloud CLI profiles (AWS)'})
        self.assertEqual(Config.from_dict(c.to_dict()).credentials.cloud.aws, ['base'])

    def test_disabled_forwarding_never_reads_host_credentials(self):
        """The global switch prevents collection as well as mounting."""
        with mock.patch.object(aws_cloud, 'read', side_effect=AssertionError('read host')):
            self.assertEqual(cloud.sync(config('default', enabled=False), 'ws'), [])
        self.assertEqual(cloud.mounts(config('default', enabled=False), 'ws'), [])

    def test_role_closure_preserves_settings_and_excludes_other_accounts(self):
        """A role needs its source account, but unrelated profiles and sessions must not leak."""
        before = self.host_bytes()
        self.assertEqual(cloud.sync(config('role'), 'ws'), [])
        rendered = self.box('config').read_text()
        self.assertIn('[profile role]', rendered)
        self.assertIn('[profile base]', rendered)
        self.assertIn('[services local]', rendered)
        self.assertIn('endpoint_url = http://localhost:9000', rendered)
        self.assertIn('region = eu-west-2', rendered)
        self.assertNotIn('hidden', rendered)
        self.assertNotIn('SECRET-HIDDEN', self.box('credentials').read_text())
        self.assertEqual(before, self.host_bytes())
        self.assertEqual(len(cloud.mounts(config('role'), 'ws')), 1)
        self.assertEqual(cloud.mounts(config('role'), 'ws')[0], (self.box(), self.host, False))
        self.assertFalse(cloud.runtime_dir('ws').exists())
        self.assertEqual(self.box('credentials').stat().st_mode & 0o777, 0o600)

    def test_menu_discloses_additional_access_without_values(self):
        """Selection must explain source accounts and shared tokens before granting access."""
        names, details, marks, problems = aws_cloud.choices(['gone'])
        self.assertEqual(problems, [])
        self.assertIn('base', details[names.index('role')])
        self.assertIn('every account/role', details[names.index('team')])
        self.assertIn('DPoP private key', details[names.index('default')])
        self.assertEqual(marks[names.index('base')], 'session credentials')
        self.assertEqual(marks[-1], 'missing')
        self.assertNotIn('SECRET', str((details, marks, problems)))

    def test_only_referenced_sso_and_login_caches_are_copied(self):
        """Refresh material is necessary, but unrelated token and role caches expose other identities."""
        self.assertEqual(cloud.sync(config('team', 'default'), 'ws'), [])
        self.assertEqual(self.box('sso/cache', self.sso.name).read_bytes(), self.sso.read_bytes())
        self.assertEqual(self.box('login/cache', self.login.name).read_bytes(), self.login.read_bytes())
        self.assertEqual(list(self.box('sso/cache').iterdir()), [self.box('sso/cache', self.sso.name)])
        self.assertNotIn('hidden', self.box('config').read_text())
        self.assertNotIn('SECRET', json.dumps(config('team', 'default').to_dict()))

    def test_legacy_sso_uses_start_url_cache_key(self):
        """Legacy SSO has no session section and hashes the start URL instead."""
        text = CONFIG.replace('sso_session = work', 'sso_start_url = https://example.awsapps.com/start\nsso_region = eu-west-1')
        (self.host / 'config').write_text(text)
        legacy = self.cache('sso', 'https://example.awsapps.com/start', json.loads(self.sso.read_text()))
        self.assertEqual(cloud.sync(config('team'), 'ws'), [])
        self.assertEqual([p.name for p in self.box('sso/cache').iterdir()], [legacy.name])

    def test_quoted_sso_session_names_keep_their_reference(self):
        """AWS permits quoted section names; rendering must not turn one name into several tokens."""
        (self.host / 'config').write_text(CONFIG.replace('sso_session = work', 'sso_session = work team')
                                        .replace('[sso-session work]', '[sso-session "work team"]'))
        token = self.cache('sso', 'work team', json.loads(self.sso.read_text()))
        self.assertEqual(cloud.sync(config('team'), 'ws'), [])
        self.assertIn("[sso-session 'work team']", self.box('config').read_text())
        self.assertEqual([p.name for p in self.box('sso/cache').iterdir()], [token.name])

    def test_credentials_file_wins_and_default_settings_do_not_leak(self):
        """The shared credentials file has AWS precedence; named profiles never inherit default secrets."""
        (self.host / 'config').write_text(CONFIG.replace('[profile base]\n',
            '[profile base]\naws_access_key_id = CONFIG-KEY\naws_secret_access_key = CONFIG-SECRET\n')
            + '[DEFAULT]\naws_access_key_id = SECRET-INHERITED\naws_secret_access_key = SECRET-INHERITED-KEY\n')
        found = aws_cloud.read()
        self.assertEqual(found.values('base')['aws_access_key_id'], 'SECRET-KEY')
        self.assertNotIn('aws_access_key_id', found.values('team'))
        self.assertEqual(cloud.sync(config('base'), 'ws'), [])
        self.assertNotIn('SECRET-INHERITED', self.box('config').read_text())

    def test_removed_or_invalid_signins_remove_stale_copies_and_role_cache(self):
        """A removed host sign-in must not remain usable through a previously rendered cache."""
        cloud.sync(config('team', 'default', 'role'), 'ws')
        self.box('cli/cache/stale.json').write_text('SECRET-ASSUMED')
        self.sso.unlink()
        self.login.write_text('INVALID-SECRET-JSON')
        (self.host / 'credentials').unlink()
        warnings = cloud.sync(config('team', 'default', 'role'), 'ws')
        self.assertIn('cache missing', str(warnings))
        self.assertIn('cache unreadable', str(warnings))
        self.assertNotIn('SECRET', str(warnings))
        for kind in ('sso', 'login', 'cli'):
            self.assertEqual(list(self.box(kind, 'cache').iterdir()), [])
        self.assertNotIn('[profile role]', self.box('config').read_text())
        self.assertEqual(self.box('credentials').read_text(), '')

    def test_cycles_missing_sources_and_host_auth_mechanisms_are_not_forwarded(self):
        """Unsupported providers cannot silently fall back to other credentials in the box."""
        with (self.host / 'config').open('a') as f:
            f.write('[profile loop]\nrole_arn = x\nsource_profile = loop\n'
                    '[profile metadata]\ncredential_source = Ec2InstanceMetadata\n'
                    '[profile web]\nweb_identity_token_file = /host/SECRET\n'
                    '[profile missing]\nrole_arn = x\nsource_profile = absent\n')
        warnings = cloud.sync(config('loop', 'metadata', 'web', 'process', 'mfa', 'missing'), 'ws')
        self.assertEqual(len(warnings), 6)
        self.assertIn('cycle', str(warnings))
        self.assertNotIn('SECRET', str(warnings))
        self.assertEqual(self.box('config').read_text(), '')
        self.assertEqual(self.box('credentials').read_text(), '')

    def test_self_source_static_role_and_multihop_roles_work(self):
        """AWS permits self-source static credentials and transitive role chains."""
        with (self.host / 'config').open('a') as f:
            f.write('[profile final]\nrole_arn = x\nsource_profile = role\n'
                    '[profile self]\nrole_arn = y\nsource_profile = self\n'
                    'aws_access_key_id = SECRET-SELF\naws_secret_access_key = SECRET-SELF-KEY\n')
        self.assertEqual(cloud.sync(config('final', 'self'), 'ws'), [])
        text = self.box('config').read_text()
        for name in ('final', 'role', 'base', 'self'):
            self.assertIn(f'[profile {name}]', text)

    def test_malformed_ini_diagnostics_cannot_echo_secret_lines(self):
        """ConfigParser's exception text includes input and is unsafe to print."""
        (self.host / 'credentials').write_text('SECRET-BROKEN-FILE')
        warnings = cloud.sync(config('base'), 'ws')
        self.assertIn('MissingSectionHeaderError', str(warnings))
        self.assertNotIn('SECRET', str(warnings))

    def test_expired_cache_warns_but_keeps_refresh_material(self):
        """An expired access token can still have usable refresh material; do not pretend login is proven."""
        self.sso.write_text(self.sso.read_text().replace('2099', '2000'))
        warnings = cloud.sync(config('team'), 'ws')
        self.assertIn('expired', str(warnings))
        self.assertIn('SECRET-REFRESH', self.box('sso/cache', self.sso.name).read_text())

    def test_box_symlinks_cannot_redirect_writes_or_cleanup_to_host(self):
        """Every writable directory component and destination file is controlled by the box."""
        before = self.host_bytes()
        cloud.sync(config('team', 'default'), 'ws')
        import shutil
        for part, target in [('sso', self.host / 'sso'), ('login/cache', self.host / 'login/cache')]:
            shutil.rmtree(self.box(part))
            self.box(part).symlink_to(target)
        self.box('config').unlink()
        self.box('config').symlink_to(self.host / 'config')
        self.assertEqual(cloud.sync(config('team', 'default'), 'ws'), [])
        self.assertEqual(before, self.host_bytes())
        shutil.rmtree(self.box())
        self.box().symlink_to(self.host)
        cloud.sync(config('team'), 'ws')
        self.assertEqual(before, self.host_bytes())
        cloud.sync(config(), 'ws')
        self.assertFalse(self.box().exists())
        self.assertEqual(before, self.host_bytes())

    def test_eks_profile_selection_and_diagnostics(self):
        """CLI arguments win over exec env, and the implicit AWS profile is literally default."""
        template = 'kind: Config\nusers:\n- name: eks\n  user:\n    exec:\n      command: aws\n      args:\n      - eks\n      - get-token\n'
        self.assertEqual(kubeconfigs.describe(template).aws_profiles, ['default'])
        env = template + '      env:\n      - name: AWS_PROFILE\n        value: team\n'
        self.assertEqual(kubeconfigs.describe(env).aws_profiles, ['team'])
        args = env.replace('      - get-token\n', '      - get-token\n      - --profile=role\n')
        summary = kubeconfigs.describe(args)
        self.assertEqual(summary.aws_profiles, ['role'])
        c = config('role')
        self.assertIn('tool group', str(kubeconfigs._aws_warnings(c, summary)))
        c.box.tools.groups['aws'] = True
        self.assertEqual(kubeconfigs._aws_warnings(c, summary), [])
        self.assertIn('not forwarded', str(kubeconfigs._aws_warnings(config('team'), summary)))
        self.assertIn('unsupported executable path', str(kubeconfigs.describe(template.replace('command: aws', 'command: /opt/aws')).warnings))
        override = env + '      - name: AWS_SECRET_ACCESS_KEY\n        value: SECRET-ENV\n'
        self.assertIn('AWS_SECRET_ACCESS_KEY', str(kubeconfigs.describe(override).warnings))
        self.assertNotIn('SECRET-ENV', str(kubeconfigs.describe(override)))

    def test_launch_diagnostics_report_tool_selection(self):
        """Forwarded AWS credentials need a matching image group, just like yc."""
        c = config('base')
        self.assertIn('aws tool group', str(runtime.tool_warnings(c)))
