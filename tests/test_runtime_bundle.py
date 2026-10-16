"""A tool reinstall must not replace Python files bind-mounted by a live box."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_box import runtime_bundle
from agent_box.paths import PACKAGE_ROOT


class RuntimeBundleTests(unittest.TestCase):
    def test_reinstall_preserves_old_bytes_and_changes_runtime_identity(self):
        """Reinstalling into the same tool directory must not alter a live bundle."""
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'installed/agent_box'
            source.mkdir(parents=True)
            (source / '__init__.py').write_text('value = 1\n')
            root = Path(tmp) / 'runtime'
            first = runtime_bundle.prepare(source, root)
            self.assertEqual(runtime_bundle.prepare(source, root), first)
            (source / '__init__.py').write_text('value = 2\n')
            second = runtime_bundle.prepare(source, root)
            self.assertNotEqual(first, second)
            self.assertEqual((first / '__init__.py').read_text(), 'value = 1\n')
            self.assertEqual((second / '__init__.py').read_text(), 'value = 2\n')
            self.assertNotEqual(runtime_bundle.identity(first), runtime_bundle.identity(second))

    def test_bytecode_does_not_change_identity_or_enter_bundle(self):
        """Importing a module must not make a compatible running box look outdated."""
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'installed/agent_box'
            source.mkdir(parents=True)
            (source / '__init__.py').write_text('')
            before = runtime_bundle.identity(source)
            (source / '__pycache__').mkdir()
            (source / '__pycache__/module.cpython-314.pyc').write_bytes(b'cache')
            bundle = runtime_bundle.prepare(source, Path(tmp) / 'runtime')
            self.assertEqual(runtime_bundle.identity(source), before)
            self.assertFalse((bundle / '__pycache__').exists())

    def test_copied_package_runs_without_checkout_or_host_virtualenv(self):
        """Container Python must be able to execute the installed runtime by itself."""
        with tempfile.TemporaryDirectory() as tmp:
            bundle = runtime_bundle.prepare(PACKAGE_ROOT, Path(tmp) / 'runtime')
            import os
            env = {name: value for name, value in os.environ.items()
                   if not name.startswith('AGENT_BOX_') and name != 'PYTHONPATH'}
            env['PYTHONPATH'] = str(bundle.parent)
            result = subprocess.run([sys.executable, '-m', 'agent_box', 'selftest'],
                                    cwd=tmp, env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((bundle / 'Containerfile').is_file())
            self.assertTrue((bundle / '.containerignore').is_file())
            self.assertTrue((bundle / 'container-launcher').stat().st_mode & 0o111)

    def test_host_wrappers_ignore_a_workspace_package_with_the_same_name(self):
        """External and Remote Control launches must load the installed runtime."""
        from agent_box import runtime
        from agent_box.config import Config
        with tempfile.TemporaryDirectory() as tmp:
            shadow = Path(tmp) / 'agent_box'
            shadow.mkdir()
            (shadow / '__init__.py').write_text('raise RuntimeError("workspace package imported")\n')
            (shadow / '__main__.py').write_text('raise RuntimeError("workspace entry point imported")\n')
            config = Config()
            config.agents.claude.remote_control = True
            commands = [runtime.external_command(['claude'], 'claude', 'example', container=False),
                        runtime.remote_control_command(['claude'], 'claude', config, container=False)]
            for command in commands:
                with self.subTest(command=command):
                    marker = '_external' if '_external' in command else '_session'
                    result = subprocess.run([*command[:command.index(marker)], 'selftest'],
                                            cwd=tmp, text=True, capture_output=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('self-tests passed', result.stdout)
