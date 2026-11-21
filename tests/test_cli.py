"""Unit tests and real popup tests on private tmux servers and pseudo-terminals."""

import argparse
import fcntl
import os
from pathlib import Path
import pty
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from unittest import mock

from tmux_scratch_session import cli


ROOT = Path(__file__).resolve().parents[1]
TMUX = shutil.which("tmux")


def terminal_session() -> None:
    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


@unittest.skipUnless(TMUX, "tmux is required for integration tests")
class IntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="tss-test-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.home = self.directory / "home"
        self.home.mkdir()
        self.socket = str(self.directory / "socket,with,commas")
        self.terminals = []
        self.environment = dict(os.environ)
        for name in ("TMUX", "TMUX_PANE", "TMUX_TMPDIR"):
            self.environment.pop(name, None)
        self.environment.update({
            "HOME": str(self.home), "TERM": "xterm-256color",
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_STATE_HOME": str(self.home / ".state"),
            "PYTHONPATH": str(ROOT / "src"),
        })
        self.addCleanup(self.stop_server)
        self.tmux(
            "new-session", "-d", "-s", "first", "-c", str(self.home),
            sys.executable, "-c", "import time; time.sleep(600)", start=True,
        )
        self.tmux("set-option", "-g", "default-shell", shutil.which("sh"))
        self.tmux(
            "set-option", "-g", "default-command",
            shlex.join([sys.executable, "-c", "import time; time.sleep(600)"]),
        )
        pid = self.tmux("display-message", "-p", "#{pid}").strip()
        session = self.tmux("display-message", "-p", "-t", "=first:", "#{session_id}").strip()
        pane = self.tmux("display-message", "-p", "-t", "=first:", "#{pane_id}").strip()
        self.environment.update({"TMUX": f"{self.socket},{pid},{session[1:]}", "TMUX_PANE": pane})
        self.target = cli.Tmux(TMUX, self.socket)

    def stop_server(self):
        subprocess.run(
            [TMUX, "-N", "-S", self.socket, "kill-server"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )

    def tmux(self, *arguments, start=False):
        options = ["-f", "/dev/null"] if start else ["-N"]
        result = subprocess.run(
            [TMUX, "-S", self.socket, *options, *arguments],
            env=self.environment, capture_output=True, check=False, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        return result.stdout.decode("utf-8", errors="surrogateescape")

    def drain(self):
        for _process, master in self.terminals:
            while True:
                try:
                    if not os.read(master, 65536):
                        break
                except (BlockingIOError, OSError):
                    break

    def wait_for(self, predicate):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            self.drain()
            if predicate():
                return
            time.sleep(0.02)
        self.fail("timed out waiting for private tmux test state")

    def attached(self):
        output = self.tmux("list-clients", "-F", "#{client_name}\t#{client_pid}\t#{client_session}")
        return [line.split("\t") for line in output.splitlines()]

    def attach_terminal(self, session="first"):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        environment = dict(self.environment)
        environment.pop("TMUX", None)
        environment.pop("TMUX_PANE", None)
        try:
            process = subprocess.Popen(
                [TMUX, "-N", "-S", self.socket, "attach-session", "-t", "=" + session],
                stdin=slave, stdout=slave, stderr=slave, env=environment,
                preexec_fn=terminal_session,
            )
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        os.set_blocking(master, False)
        self.terminals.append((process, master))

        def cleanup():
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            os.close(master)

        self.addCleanup(cleanup)
        self.wait_for(lambda: any(pid == str(process.pid) for _name, pid, _session in self.attached()))
        return next(name for name, pid, _session in self.attached() if pid == str(process.pid))

    def launch(self, *arguments, environment=None):
        installed = os.environ.get("TSS_TEST_EXECUTABLE")
        command = [installed] if installed else [sys.executable, "-m", "tmux_scratch_session"]
        child_environment = dict(environment or self.environment)
        if installed:
            child_environment.pop("PYTHONPATH", None)
        process = subprocess.Popen(
            [*command, "--tmux", TMUX, *map(str, arguments)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=child_environment,
        )

        def cleanup():
            if process.poll() is None:
                process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()

        self.addCleanup(cleanup)
        return process

    def assert_result(self, process, *, success=True):
        output, error = process.communicate(timeout=10)
        if success:
            self.assertEqual(process.returncode, 0, error.decode(errors="replace"))
            self.assertEqual(output, b"")
        else:
            self.assertNotEqual(process.returncode, 0)
            self.assertNotIn(b"Traceback", error)
        return error.decode(errors="replace")

    def wait_for_popup(self, process):
        def ready():
            if any(session == cli.SESSION_NAME for _name, _pid, session in self.attached()):
                return True
            if process.poll() is not None and process.returncode:
                _output, error = process.communicate()
                self.fail(error.decode(errors="replace"))
            return False
        self.wait_for(ready)

    def close_popup(self, client):
        self.tmux("display-popup", "-C", "-c", client)
        self.wait_for(lambda: all(session != cli.SESSION_NAME for _name, _pid, session in self.attached()))

    def test_real_popup_creates_and_reuses_persistent_session_without_switching_outer_client(self):
        client = self.attach_terminal()
        bindings_before = self.tmux("list-keys")
        first = self.launch()
        self.wait_for_popup(first)
        self.assertEqual(next(session for name, _pid, session in self.attached() if name == client), "first")
        self.assertEqual(self.tmux("show-options", "-v", "-t", "=zzzzzzzz:", "status").strip(), "off")
        self.assertEqual(self.tmux("show-options", "-gv", "status").strip(), "on")
        self.assertEqual(
            self.tmux("display-message", "-p", "-t", "=zzzzzzzz:", "#{pane_current_path}").strip(),
            str(self.home),
        )
        session = self.tmux("display-message", "-p", "-t", "=zzzzzzzz:", "#{session_id}")
        self.tmux("set-option", "-t", "=zzzzzzzz:", "@keep", "preserved")
        self.close_popup(client)
        self.assert_result(first)

        second = self.launch(
            "--width", "70%", "--height", "20", "--style", "default", "--border-style", "default",
        )
        self.wait_for_popup(second)
        self.assertEqual(self.tmux("display-message", "-p", "-t", "=zzzzzzzz:", "#{session_id}"), session)
        self.assertEqual(self.tmux("show-options", "-v", "-t", "=zzzzzzzz:", "@keep").strip(), "preserved")
        self.close_popup(client)
        self.assert_result(second)
        self.assertEqual(self.tmux("list-keys"), bindings_before)

    def test_detaching_nested_client_closes_popup_and_preserves_session(self):
        self.attach_terminal()
        process = self.launch()
        self.wait_for_popup(process)
        nested = next(name for name, _pid, session in self.attached() if session == cli.SESSION_NAME)
        self.tmux("detach-client", "-t", nested)
        self.assert_result(process)
        self.wait_for(lambda: all(session != cli.SESSION_NAME for _name, _pid, session in self.attached()))
        self.tmux("has-session", "-t", "=zzzzzzzz")

    def test_no_attached_client_is_rejected_without_creating_scratch(self):
        error = self.assert_result(self.launch(), success=False)
        self.assertIn("no attached terminal client", error)
        self.assertNotIn(cli.SESSION_NAME, self.tmux("list-sessions", "-F", "#{session_name}").splitlines())

    def test_ambiguous_clients_require_explicit_target(self):
        self.attach_terminal()
        second = self.attach_terminal()
        error = self.assert_result(self.launch(), success=False)
        self.assertIn("multiple clients", error)
        process = self.launch("--client", second)
        self.wait_for_popup(process)
        self.close_popup(second)
        self.assert_result(process)

    def test_other_session_client_is_not_selected(self):
        first = self.attach_terminal()
        self.tmux("new-session", "-d", "-s", "second")
        other = self.attach_terminal("second")
        error = self.assert_result(self.launch("--client", other), success=False)
        self.assertIn("calling session", error)
        process = self.launch()
        self.wait_for_popup(process)
        self.assertEqual(next(session for name, _pid, session in self.attached() if name == other), "second")
        self.close_popup(first)
        self.assert_result(process)

    def scratch_environment(self):
        session, pane = self.tmux(
            "display-message", "-p", "-t", "=zzzzzzzz:", "#{session_id}\t#{pane_id}",
        ).strip().split("\t")
        environment = dict(self.environment)
        environment["TMUX"] = self.environment["TMUX"].rsplit(",", 1)[0] + "," + session[1:]
        environment["TMUX_PANE"] = pane
        return environment

    def test_invoking_inside_scratch_detaches_popup_without_changing_session_state(self):
        outer = self.attach_terminal()
        popup = self.launch()
        self.wait_for_popup(popup)
        before = self.tmux(
            "display-message", "-p", "-t", "=zzzzzzzz:", "#{session_id} #{pane_id} #{pane_pid}",
        )
        self.tmux("set-option", "-t", "=zzzzzzzz:", "status", "on")
        # These dimensions cannot fit; detachment must skip popup validation.
        detach = self.launch("--width", "1%", "--height", "99999", environment=self.scratch_environment())
        self.assert_result(detach)
        self.assert_result(popup)
        self.wait_for(lambda: all(session != cli.SESSION_NAME for _name, _pid, session in self.attached()))
        self.assertEqual(self.tmux(
            "display-message", "-p", "-t", "=zzzzzzzz:", "#{session_id} #{pane_id} #{pane_pid}",
        ), before)
        self.assertEqual(self.tmux("show-options", "-v", "-t", "=zzzzzzzz:", "status").strip(), "on")
        self.assertEqual(next(session for name, _pid, session in self.attached() if name == outer), "first")

    def test_inside_scratch_requires_unambiguous_client_and_preserves_other_clients(self):
        outer = self.attach_terminal()
        popup = self.launch()
        self.wait_for_popup(popup)
        nested = next(name for name, _pid, session in self.attached() if session == cli.SESSION_NAME)
        other = self.attach_terminal(cli.SESSION_NAME)
        environment = self.scratch_environment()
        error = self.assert_result(self.launch(environment=environment), success=False)
        self.assertIn("multiple clients", error)
        error = self.assert_result(self.launch("--client", outer, environment=environment), success=False)
        self.assertIn("calling session", error)
        self.assertEqual(sum(session == cli.SESSION_NAME for _name, _pid, session in self.attached()), 2)

        self.assert_result(self.launch("--client", nested, environment=environment))
        self.assert_result(popup)
        self.assertEqual(
            [name for name, _pid, session in self.attached() if session == cli.SESSION_NAME], [other],
        )
        self.tmux("has-session", "-t", "=zzzzzzzz")

    def test_direct_scratch_terminal_detaches_without_opening_popup(self):
        self.tmux("new-session", "-d", "-s", "zzzzzzzz")
        client = self.attach_terminal(cli.SESSION_NAME)
        self.assert_result(self.launch(environment=self.scratch_environment()))
        self.wait_for(lambda: all(name != client for name, _pid, _session in self.attached()))
        self.tmux("has-session", "-t", "=zzzzzzzz")

    def test_inside_unattached_scratch_refuses_to_guess_a_client(self):
        self.tmux("new-session", "-d", "-s", "zzzzzzzz")
        error = self.assert_result(self.launch(environment=self.scratch_environment()), success=False)
        self.assertIn("no attached terminal client", error)
        self.tmux("has-session", "-t", "=zzzzzzzz")

    def test_outside_tmux_is_rejected(self):
        environment = dict(self.environment)
        environment.pop("TMUX")
        error = self.assert_result(self.launch(environment=environment), success=False)
        self.assertIn("inside tmux", error)

    def test_stale_server_identity_is_rejected(self):
        environment = dict(self.environment, TMUX=f"{self.socket},1,0")
        error = self.assert_result(self.launch(environment=environment), success=False)
        self.assertIn("does not identify the running server", error)

    def test_invalid_dimensions_and_unknown_session_option_are_rejected(self):
        for arguments in (("--width", "0%"), ("--height", "abc"), ("--session", "different")):
            with self.subTest(arguments=arguments):
                self.assert_result(self.launch(*arguments), success=False)
        self.assertNotIn(cli.SESSION_NAME, self.tmux("list-sessions", "-F", "#{session_name}").splitlines())

    def test_missing_executable_is_actionable(self):
        error = self.assert_result(self.launch("--tmux", "/no/such/tmux"), success=False)
        self.assertIn("executable not found", error)

    def test_exact_session_name_and_semicolon_directory(self):
        directory = self.directory / "spaces and a trailing semicolon;"
        directory.mkdir()
        self.tmux("new-session", "-d", "-s", "zzzzzzzz-neighbor")
        context = cli.Context(self.target, cli.Client("unused", "$0", "%0"), "%0", str(directory))
        cli.ensure_session(context)
        self.assertEqual(
            self.tmux("display-message", "-p", "-t", "=zzzzzzzz:", "#{pane_current_path}").strip(),
            str(directory),
        )
        self.assertEqual(self.target.run("display-message", "-p", "literal;"), "literal;\n")
        self.assertEqual(self.target.run("display-message", "-p", "literal\\;"), "literal\\;\n")

    def test_too_small_popup_is_rejected_before_session_creation(self):
        self.attach_terminal()
        # One percent of our 120-column terminal cannot fit a bordered popup.
        error = self.assert_result(self.launch("--width", "1%"), success=False)
        self.assertIn("does not fit", error)
        self.assertNotIn(cli.SESSION_NAME, self.tmux("list-sessions", "-F", "#{session_name}").splitlines())

    def test_popup_launch_failure_leaves_scratch_session_running(self):
        self.attach_terminal()
        wrapper = self.directory / "fail-popup"
        wrapper.write_text(
            f"#!{sys.executable}\nimport os, sys\n"
            "if sys.argv[1:2] == ['-N'] and sys.argv[4:5] == ['display-popup']:\n"
            "    print('simulated popup failure', file=sys.stderr)\n"
            "    sys.exit(1)\n"
            f"os.execv({TMUX!r}, [{TMUX!r}, *sys.argv[1:]])\n"
        )
        wrapper.chmod(0o700)
        error = self.assert_result(self.launch("--tmux", wrapper), success=False)
        self.assertIn("simulated popup failure", error)
        self.tmux("has-session", "-t", "=zzzzzzzz")

    def test_other_server_is_untouched(self):
        other = str(self.directory / "other-socket")
        subprocess.run(
            [TMUX, "-S", other, "-f", "/dev/null", "new-session", "-d", "-s", "other",
             sys.executable, "-c", "import time; time.sleep(600)"],
            env=self.environment, check=True, capture_output=True,
        )
        self.addCleanup(lambda: subprocess.run(
            [TMUX, "-N", "-S", other, "kill-server"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        ))
        client = self.attach_terminal()
        process = self.launch()
        self.wait_for_popup(process)
        output = subprocess.run(
            [TMUX, "-N", "-S", other, "list-sessions", "-F", "#{session_name}"],
            check=True, capture_output=True,
        ).stdout
        self.assertEqual(output, b"other\n")
        self.close_popup(client)
        self.assert_result(process)

    def test_missing_pane_environment_uses_selected_clients_pane(self):
        client = self.attach_terminal()
        environment = dict(self.environment)
        environment.pop("TMUX_PANE")
        process = self.launch(environment=environment)
        self.wait_for_popup(process)
        self.close_popup(client)
        self.assert_result(process)


class UnitTests(unittest.TestCase):
    def test_dimensions(self):
        for value in ("3", "80", "1%", "80%", "100%"):
            self.assertEqual(cli.dimension(value), value)
        for value in ("", "0", "2", "0%", "101%", "-1", "3.5", "10;", "80%x"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                cli.dimension(value)

    def test_old_tmux_and_invalid_environment_are_rejected(self):
        for environment, identity, message in (
            ({"TMUX": "bad"}, "123 3.8", "valid \\$TMUX"),
            ({"TMUX": "/socket,123,0"}, "123 3.7", r"tmux 3\.8\+"),
            ({"TMUX": "/socket,123,0", "TMUX_PANE": "invalid"}, "123 3.8", r"invalid \$TMUX_PANE"),
        ):
            with (
                self.subTest(environment=environment, identity=identity),
                mock.patch.dict(os.environ, environment, clear=True),
                mock.patch.object(cli.shutil, "which", return_value="/tmux"),
                mock.patch.object(cli.Tmux, "run", side_effect=[identity, "first\n"]),
                self.assertRaisesRegex(cli.ScratchError, message),
            ):
                cli.resolve_context("tmux", None)

    def test_resolving_scratch_context_does_not_query_working_directory(self):
        with (
            mock.patch.dict(os.environ, {"TMUX": "/socket,123,1", "TMUX_PANE": "%1"}, clear=True),
            mock.patch.object(cli.shutil, "which", return_value="/tmux"),
            mock.patch.object(cli.Tmux, "run", side_effect=[
                "123 3.8", "zzzzzzzz\n", "terminal\t$1\t%1\t0\n",
            ]) as run,
        ):
            context = cli.resolve_context("tmux", None)
        self.assertEqual(context.session_name, cli.SESSION_NAME)
        self.assertEqual(context.directory, "")
        self.assertEqual(run.call_count, 3)

    def test_detach_failure_is_reported_without_falling_back_to_opening_popup(self):
        tmux = mock.Mock()
        tmux.run.side_effect = cli.ScratchError("detach failed")
        context = cli.Context(tmux, cli.Client("terminal", "$1", "%1"), "%1", "", cli.SESSION_NAME)
        with (
            mock.patch.object(sys, "argv", ["tmux-scratch-session"]),
            mock.patch.object(cli, "resolve_context", return_value=context),
            mock.patch.object(cli, "validate_popup_size") as validate,
            mock.patch.object(cli, "ensure_session") as ensure,
            mock.patch.object(cli, "open_popup") as open_popup,
            mock.patch("builtins.print") as report,
        ):
            self.assertEqual(cli.main(), 1)
        tmux.run.assert_called_once_with("detach-client", "-t", "terminal")
        validate.assert_not_called()
        ensure.assert_not_called()
        open_popup.assert_not_called()
        self.assertIn("detach failed", report.call_args.args[0])

    def test_popup_size_fits_selected_terminal(self):
        tmux = mock.Mock()
        tmux.run.return_value = "120 40\n"
        context = cli.Context(tmux, cli.Client("one", "$0", "%0"), "%0", "/directory")
        cli.validate_popup_size(context, argparse.Namespace(width="80%", height="25"))
        for width, height in (("1%", "80%"), ("80%", "100")):
            with self.subTest(width=width, height=height), self.assertRaises(cli.ScratchError):
                cli.validate_popup_size(context, argparse.Namespace(width=width, height=height))
        tmux.run.return_value = "invalid"
        with self.assertRaisesRegex(cli.ScratchError, "terminal dimensions"):
            cli.validate_popup_size(context, argparse.Namespace(width="80%", height="80%"))

    def test_semicolon_escape_preserves_existing_backslashes(self):
        self.assertEqual(cli.literal_argument("literal;"), "literal\\;")
        self.assertEqual(cli.literal_argument("literal\\;"), "literal\\\\;")
        self.assertEqual(cli.literal_argument("a;b"), "a;b")

    def test_selects_only_terminal_clients_and_calling_session(self):
        tmux = mock.Mock()
        tmux.run.return_value = "tty1\t$0\t%0\t0\ncontrol\t$0\t%0\t1\ntty2\t$1\t%1\t0\n"
        attached = cli.clients(tmux)
        self.assertEqual(cli.choose_client(attached, "$0", "%0", None).name, "tty1")

    def test_distinct_active_pane_disambiguates_clients(self):
        attached = [cli.Client("one", "$0", "%0"), cli.Client("two", "$0", "%1")]
        self.assertEqual(cli.choose_client(attached, "$0", "%1", None).name, "two")
        self.assertEqual(cli.choose_client(attached, "$0", "%1", "one").name, "one")
        with self.assertRaisesRegex(cli.ScratchError, "multiple clients"):
            cli.choose_client(attached, "$0", "%2", None)

    def test_malformed_client_information_is_rejected(self):
        tmux = mock.Mock()
        tmux.run.return_value = "truncated\n"
        with self.assertRaises(cli.ScratchError):
            cli.clients(tmux)

    def test_creation_race_reuses_session_created_by_other_client(self):
        tmux = mock.Mock()
        tmux.run.side_effect = ["first\n", cli.ScratchError("duplicate session"), "first\nzzzzzzzz\n", ""]
        context = cli.Context(tmux, cli.Client("one", "$0", "%0"), "%0", "/directory")
        cli.ensure_session(context)
        self.assertEqual(tmux.run.call_args.args, ("set-option", "-t", "=zzzzzzzz:", "status", "off"))

    def test_real_creation_failure_is_not_hidden(self):
        for following in ("first\n", cli.ScratchError("server stopped")):
            tmux = mock.Mock()
            tmux.run.side_effect = ["first\n", cli.ScratchError("creation failed"), following]
            context = cli.Context(tmux, cli.Client("one", "$0", "%0"), "%0", "/directory")
            with (
                self.subTest(following=following),
                self.assertRaisesRegex(cli.ScratchError, "creation failed"),
            ):
                cli.ensure_session(context)

    def test_existing_session_is_reused_without_new_session_command(self):
        tmux = mock.Mock()
        tmux.run.side_effect = ["first\nzzzzzzzz\n", ""]
        context = cli.Context(tmux, cli.Client("one", "$0", "%0"), "%0", "/different-directory")
        cli.ensure_session(context)
        self.assertEqual(tmux.run.call_count, 2)

    def test_popup_has_exact_client_socket_and_unset_only_at_attachment(self):
        tmux = mock.Mock(executable="/tmux", socket="/socket,with,commas")
        context = cli.Context(tmux, cli.Client("terminal", "$0", "%0"), "%0", "/directory")
        arguments = argparse.Namespace(width="80%", height="70%", style="default", border_style="default")
        cli.open_popup(context, arguments)
        command = tmux.run.call_args.args
        self.assertEqual(command[:9], (
            "display-popup", "-E", "-c", "terminal", "-t", "%0", "-d", "/directory", "-w",
        ))
        self.assertEqual(command[-7:], (
            "/tmux", "-N", "-S", "/socket,with,commas", "attach-session", "-t", "=zzzzzzzz",
        ))
        with mock.patch.dict(os.environ, {"TMUX": "outer"}):
            with mock.patch.object(os, "execv") as execute:
                with mock.patch.object(sys, "argv", ["-c", "/tmux", "-S", "/socket"]):
                    exec(cli.ATTACH_PROGRAM)
                self.assertNotIn("TMUX", os.environ)
                execute.assert_called_once_with("/tmux", ["/tmux", "-S", "/socket"])


if __name__ == "__main__":
    unittest.main()
