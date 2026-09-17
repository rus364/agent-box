import argparse
import ast
import contextlib
import inspect
import io
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
import unittest.mock
from pathlib import Path

from agent_box import cli, completion
from agent_box.errors import AgentBoxError
from agent_box.launch_options import FLAGS

SHIM = Path(__file__).resolve().parents[1] / "agent-box"


def words(line, environ=None):
    """Candidate words for a command line, as `_complete` would print them after the mode line."""
    output = io.StringIO()
    completion.main(line.split(" "), environ or {}, output)
    mode, *lines = output.getvalue().splitlines()
    return mode, [entry.split("\t")[0] for entry in lines]


class CandidateTests(unittest.TestCase):
    def test_every_launch_option_is_offered_in_each_accepted_spelling(self):
        """The option table is the source, so a new option completes without a completion change."""
        self.assertEqual(set(words("-")[1]), set(FLAGS))

    def test_every_dispatched_command_has_a_completion_entry(self):
        """A command added to cli.main without an entry here would silently never complete.

        The names are read from main's own `args[0]` comparisons, so a new dispatch
        branch counts even when it is not in NON_RUNTIME, as `ext` is not.
        """
        dispatched = set(cli.HOST_ONLY_AGENTS)
        for node in ast.walk(ast.parse(textwrap.dedent(inspect.getsource(cli.main)))):
            if isinstance(node, ast.Compare) and ast.unparse(node.left) == "args[0]":
                for part in (part for comparator in node.comparators for part in ast.walk(comparator)):
                    if isinstance(part, ast.Constant) and isinstance(part.value, str):
                        dispatched.add(part.value)
                    elif isinstance(part, ast.Name):
                        dispatched |= set(getattr(cli, part.id))
        self.assertTrue({"ext", "shell", "attach", "completion"} <= dispatched)
        self.assertEqual(dispatched - set(completion.COMMANDS) - completion.HIDDEN, set())
        offered = set(words("")[1])
        self.assertEqual(offered & completion.HIDDEN, set())
        self.assertIn("completion", offered)

    def test_option_values_come_from_their_choices_never_placeholders(self):
        self.assertEqual(words("--review ")[1], ["off", "auto", "always"])
        self.assertEqual(words("--worker ")[1], ["auto", "direct", "delegated"])
        self.assertEqual(words("--review=al")[1], ["--review=always"])
        self.assertEqual(words("--tracker ")[1], [])
        self.assertEqual(words("--workspace ")[0], "dirs")
        self.assertEqual(words("--env TO", {"TOKEN": "x", "PATH": "/bin"})[1], ["TOKEN"])

    def test_options_after_their_values_still_complete(self):
        self.assertEqual(words("--mount /x --review a")[1], ["auto", "always"])
        self.assertEqual(words("--host --no-push co")[1], ["codex", "config", "completion"])

    def test_agent_arguments_complete_as_files(self):
        """After claude/codex, or a flag that is not agent-box's, the words belong to the agent."""
        for line in ("claude -", "--host codex exec ", "--model ", "-- claude "):
            with self.subTest(line=line):
                self.assertEqual(words(line), ("files", []))

    def test_tracker_and_workflow_offer_every_subcommand_of_their_parsers(self):
        """Subcommands added without help= are still commands a person types."""
        for command, load in (("tracker", completion.tracker_parser), ("workflow", completion.workflow_parser)):
            with self.subTest(command=command):
                sub = next(action for action in load()._actions if isinstance(action, argparse._SubParsersAction))
                self.assertEqual(words(f"{command} ")[1], list(sub.choices))
        self.assertEqual(words("workflow lan")[1], ["land"])

    def test_tracker_and_workflow_complete_from_their_parsers(self):
        self.assertEqual(words("tracker state 374 Re")[1], ["Ready", "Review"])
        self.assertEqual(words("--no-tmux workflow land 374 ")[1], ["inspect", "request-review", "merge", "reconcile", "cleanup"])
        self.assertEqual(words("workflow prepare 374 --role ")[1], ["worker", "reviewer", "scout"])
        self.assertIn("--brief", words("workflow prepare 374 --role scout --")[1])
        self.assertEqual(words("workflow validate 374 -- python3 ")[0], "files")
        self.assertEqual(words("workflow validate 374 --primary claude -- python3 ")[0], "files")
        self.assertIn("--primary", words("workflow validate 374 --")[1])

    def test_a_new_argparse_subcommand_completes_without_a_completion_change(self):
        """Any command that parses with argparse gets its subcommands, options and choices for free."""
        parser = argparse.ArgumentParser()
        parser.add_argument("--verbose", action="store_true")
        sub = parser.add_subparsers()
        fresh = sub.add_parser("fresh", help="A command added later")
        fresh.add_argument("task")
        fresh.add_argument("--mode", choices=("fast", "slow"))
        fresh.add_argument("--file")
        sub.add_parser("quiet")
        self.assertEqual(completion.parser_candidates(parser, ["--verbose", ""]),
                         ("words", [("fresh", "A command added later"), ("quiet", "")]))
        self.assertEqual(completion.parser_candidates(parser, ["fresh", "1", "--mode", ""])[1], [("fast", ""), ("slow", "")])
        self.assertEqual(completion.parser_candidates(parser, ["fresh", "1", "--mode=s"])[1],
                         [("--mode=fast", ""), ("--mode=slow", "")])
        self.assertEqual(completion.parser_candidates(parser, ["fresh", "1", "--file", ""]), ("files", []))

    def test_ext_offers_only_enabled_external_providers(self):
        config = unittest.mock.Mock(external_providers={
            "deepseek": unittest.mock.Mock(enabled=True), "idle": unittest.mock.Mock(enabled=False)})
        with unittest.mock.patch("agent_box.config.effective_config", return_value=config):
            self.assertEqual(words("ext ")[1], ["deepseek"])
            self.assertEqual(words("ext deepseek ")[1], ["claude", "codex"])

    def test_simple_commands_do_not_offer_a_word_twice(self):
        self.assertEqual(words("uninstall --purge --")[1], ["--yes"])


class CompleteEntryPointTests(unittest.TestCase):
    def run_main(self, *argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with unittest.mock.patch("sys.argv", ["agent-box", *argv]), \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            cli.main()
        return stdout.getvalue(), stderr.getvalue()

    def test_tab_never_reads_the_project_configuration_or_leaves_the_process(self):
        """A broken .agent-box.json, the network or Podman must not make Tab slow or noisy."""
        from agent_box import project_config
        with unittest.mock.patch.object(project_config, "load", side_effect=AgentBoxError("broken")) as load, \
             unittest.mock.patch("subprocess.run") as run, unittest.mock.patch("subprocess.Popen") as popen, \
             unittest.mock.patch("urllib.request.urlopen") as urlopen:
            for line in (["--review", ""], ["tracker", "get", ""], ["workflow", ""], ["attach", ""]):
                with self.subTest(line=line):
                    stdout, stderr = self.run_main("_complete", *line)
                    self.assertTrue(stdout.startswith("words\n"))
                    self.assertEqual(stderr, "")
        for mock in (load, run, popen, urlopen):
            mock.assert_not_called()

    def test_a_failure_while_completing_prints_nothing(self):
        for failure in (RuntimeError("bug"), AgentBoxError("Cannot read rendered workflow configuration")):
            with self.subTest(failure=failure), \
                 unittest.mock.patch("agent_box.config.effective_config", side_effect=failure):
                self.assertEqual(self.run_main("_complete", "ext", ""), ("", ""))


class CompletionCommandTests(unittest.TestCase):
    def run_command(self, *argv):
        from agent_box import project_config
        stdout = io.StringIO()
        with unittest.mock.patch.object(project_config, "load", return_value={}), \
             unittest.mock.patch("sys.argv", ["agent-box", *argv]), \
             unittest.mock.patch.object(cli, "die", side_effect=SystemExit(1)) as die, \
             contextlib.redirect_stdout(stdout):
            try:
                cli.main()
            except SystemExit:
                pass
        return stdout.getvalue(), die

    def test_scripts_register_for_their_shell(self):
        self.assertIn("complete -F _agent_box agent-box", self.run_command("completion", "bash")[0])
        self.assertIn("compdef _agent_box agent-box", self.run_command("completion", "zsh")[0])

    def test_help_explains_loading_for_both_shells(self):
        for argv in ([], ["--help"], ["-h"], ["bash", "--help"], ["zsh", "-h"]):
            with self.subTest(argv=argv):
                output, die = self.run_command("completion", *argv)
                die.assert_not_called()
                self.assertIn("source <(agent-box completion bash)", output)
                self.assertIn("source <(agent-box completion zsh)", output)

    def test_unknown_shells_extra_arguments_and_launch_options_are_refused(self):
        for argv, message in ((["completion", "fish"], "expected: agent-box completion bash|zsh"),
                              (["completion", "bash", "zsh"], "expected: agent-box completion bash|zsh"),
                              (["--review", "off", "completion", "bash"], "not valid before completion")):
            with self.subTest(argv=argv):
                output, die = self.run_command(*argv)
                self.assertIn(message, die.call_args.args[0])
                self.assertEqual(output, "")


class ShellScriptTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        (Path(directory.name) / "agent-box").symlink_to(SHIM)
        self.env = {name: value for name, value in os.environ.items() if not name.startswith("AGENT_BOX_")}
        self.env["PATH"] = f"{directory.name}:{self.env.get('PATH', '/usr/bin:/bin')}"

    @unittest.skipUnless(shutil.which("bash"), "bash is not installed")
    def test_bash_completes_through_the_real_script(self):
        """Readline replaces only the text between the last = or : and the cursor, so candidates must too."""
        script = r"""
source <(agent-box completion bash)
ask() { COMP_LINE=$1; COMP_POINT=${2:-${#1}}; _agent_box; printf '%s,' "${COMPREPLY[@]}"; echo; }
ask 'agent-box --rev'
ask 'agent-box --review=al'
ask 'agent-box --review='
ask 'agent-box --review' 15
ask 'agent-box tracker st'
"""
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=self.env, timeout=60)
        self.assertEqual((result.stderr, result.stdout.splitlines()),
                         ("", ["--reviewer,--review,", "always,", "off,auto,always,", "--reviewer,--review,",
                               "status,state,"]))

    @unittest.skipUnless(shutil.which("zsh"), "zsh is not installed")
    def test_zsh_script_parses(self):
        script = subprocess.run([str(SHIM), "completion", "zsh"], capture_output=True, text=True, env=self.env, timeout=60)
        result = subprocess.run(["zsh", "-n"], input=script.stdout, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
