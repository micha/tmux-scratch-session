"""Open the persistent zzzzzzzz popup, or detach its selected client from inside."""

import argparse
from dataclasses import dataclass
import os
import re
import shutil
import subprocess
import sys


SESSION_NAME = "zzzzzzzz"
SESSION_TARGET = "=" + SESSION_NAME
ATTACH_PROGRAM = (
    "import os, sys\n"
    "os.environ.pop('TMUX', None)\n"
    "os.execv(sys.argv[1], sys.argv[1:])\n"
)


class ScratchError(Exception):
    """A user-facing error that does not need a traceback."""


def literal_argument(value: str) -> str:
    """Escape tmux's special handling of trailing argv semicolons."""
    return value[:-1] + r"\;" if value.endswith(";") else value


@dataclass(frozen=True)
class Tmux:
    executable: str
    socket: str

    def run(self, *arguments: str, successful_statuses: tuple[int, ...] = (0,)) -> str:
        result = subprocess.run(
            [self.executable, "-N", "-S", self.socket,
             *(literal_argument(argument) for argument in arguments)],
            capture_output=True, check=False,
        )
        output = result.stdout.decode("utf-8", errors="surrogateescape")
        error = result.stderr.decode("utf-8", errors="replace").strip()
        if result.returncode not in successful_statuses:
            detail = error or output.strip() or f"exit status {result.returncode}"
            raise ScratchError(f"tmux {arguments[0]}: {detail}")
        if error:
            print(error, file=sys.stderr)
        return output


@dataclass(frozen=True)
class Client:
    name: str
    session: str
    pane: str


@dataclass(frozen=True)
class Context:
    tmux: Tmux
    client: Client
    pane: str
    directory: str
    session_name: str = ""


def dimension(value: str) -> str:
    """Accept positive cell counts or percentages between 1 and 100."""
    if not re.fullmatch(r"[0-9]+%?", value):
        raise argparse.ArgumentTypeError("use a cell count (at least 3) or a percentage (1%-100%)")
    number = int(value.removesuffix("%"))
    if (value.endswith("%") and not 1 <= number <= 100) or (not value.endswith("%") and number < 3):
        raise argparse.ArgumentTypeError("use a cell count (at least 3) or a percentage (1%-100%)")
    return value


def clients(tmux: Tmux) -> list[Client]:
    output = tmux.run(
        "list-clients", "-F", "#{client_name}\t#{session_id}\t#{pane_id}\t#{client_control_mode}",
    )
    result = []
    for line in output.splitlines():
        fields = line.split("\t")
        if len(fields) != 4 or fields[3] not in ("0", "1"):
            raise ScratchError("could not decode attached client information")
        name, session, pane, control = fields
        if control == "0":
            result.append(Client(name, session, pane))
    return result


def choose_client(
    attached: list[Client], session: str, pane: str | None, requested: str | None,
) -> Client:
    candidates = [client for client in attached if client.session == session]
    if requested:
        candidates = [client for client in candidates if client.name == requested]
        if not candidates:
            raise ScratchError("--client must name an attached terminal client in the calling session")
        return candidates[0]
    if not candidates:
        raise ScratchError("no attached terminal client in the calling session")
    if len(candidates) > 1 and pane:
        focused = [client for client in candidates if client.pane == pane]
        if len(focused) == 1:
            return focused[0]
    if len(candidates) != 1:
        raise ScratchError(
            "multiple clients match this session; specify --client with an exact client name"
        )
    return candidates[0]


def resolve_context(executable: str, requested_client: str | None) -> Context:
    parts = os.environ.get("TMUX", "").rsplit(",", 2)
    if len(parts) != 3 or not parts[0] or not all(part.isdigit() for part in parts[1:]):
        raise ScratchError("run this command inside tmux (valid $TMUX required)")
    binary = shutil.which(executable)
    if not binary:
        raise ScratchError(f"tmux executable not found: {executable}; install tmux or use --tmux PATH")
    tmux = Tmux(binary, parts[0])
    identity = tmux.run("display-message", "-p", "#{pid} #{version}").split()
    if len(identity) != 2 or identity[0] != parts[1]:
        raise ScratchError("$TMUX does not identify the running server; use a current tmux shell")
    version = re.search(r"(\d+)\.(\d+)", identity[1])
    if not version or tuple(map(int, version.groups())) < (3, 8):
        raise ScratchError("tmux 3.8+ is required")
    session = "$" + parts[2]
    name = tmux.run("display-message", "-p", "-t", session + ":", "#{session_name}").removesuffix("\n")
    pane = os.environ.get("TMUX_PANE")
    if pane and not re.fullmatch(r"%[0-9]+", pane):
        raise ScratchError("invalid $TMUX_PANE; use a current tmux shell")
    client = choose_client(clients(tmux), session, pane, requested_client)
    pane = pane or client.pane
    directory = ""
    if name != SESSION_NAME:
        directory = tmux.run(
            "display-message", "-p", "-t", pane, "#{pane_current_path}",
        ).removesuffix("\n")
        if not directory:
            raise ScratchError("could not determine the calling pane's working directory")
    return Context(tmux, client, pane, directory, session_name=name)


def validate_popup_size(context: Context, arguments: argparse.Namespace) -> None:
    dimensions = context.tmux.run(
        "display-message", "-p", "-c", context.client.name, "#{client_width} #{client_height}",
    ).split()
    if len(dimensions) != 2 or not all(value.isdigit() for value in dimensions):
        raise ScratchError("could not determine the selected client's terminal dimensions")
    for axis, available in zip(("width", "height"), map(int, dimensions)):
        requested = getattr(arguments, axis)
        cells = int(requested[:-1]) * available // 100 if requested.endswith("%") else int(requested)
        if not 3 <= cells <= available:
            raise ScratchError(f"popup {axis} does not fit the selected client's {available}-cell terminal")


def ensure_session(context: Context) -> None:
    tmux = context.tmux
    names = tmux.run("list-sessions", "-F", "#{session_name}").splitlines()
    if SESSION_NAME not in names:
        try:
            tmux.run("new-session", "-d", "-s", SESSION_NAME, "-c", context.directory)
        except ScratchError as creation_error:
            # A different client may have created the shared session first.
            try:
                names = tmux.run("list-sessions", "-F", "#{session_name}").splitlines()
            except ScratchError:
                raise creation_error
            if SESSION_NAME not in names:
                raise creation_error
    tmux.run("set-option", "-t", SESSION_TARGET + ":", "status", "off")


def open_popup(context: Context, arguments: argparse.Namespace) -> None:
    context.tmux.run(
        "display-popup", "-E", "-c", context.client.name, "-t", context.pane,
        "-d", context.directory, "-w", arguments.width, "-h", arguments.height,
        "-s", arguments.style, "-S", arguments.border_style,
        sys.executable, "-c", ATTACH_PROGRAM,
        context.tmux.executable, "-N", "-S", context.tmux.socket,
        "attach-session", "-t", SESSION_TARGET,
        # Closing a popup terminates its nested attachment with SIGHUP.
        successful_statuses=(0, 129),
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Open the zzzzzzzz scratch popup, or detach this client when already inside it.",
        epilog="Requires tmux 3.8+. Detaching keeps the scratch session and other clients running.",
    )
    parser.add_argument("--width", type=dimension, default="80%", help="popup width (default: 80%%)")
    parser.add_argument("--height", type=dimension, default="80%", help="popup height (default: 80%%)")
    parser.add_argument("--style", default="bg=terminal", help="tmux popup style (default: bg=terminal)")
    parser.add_argument(
        "--border-style", default="fg=themelightgrey,bg=terminal",
        help="tmux border style (default: fg=themelightgrey,bg=terminal)",
    )
    parser.add_argument(
        "--client", metavar="NAME", help="exact attached client name when selection is ambiguous",
    )
    parser.add_argument("--tmux", default="tmux", metavar="PATH", help="tmux executable (default: tmux)")
    arguments = parser.parse_args()
    try:
        context = resolve_context(arguments.tmux, arguments.client)
        if context.session_name == SESSION_NAME:
            context.tmux.run("detach-client", "-t", context.client.name)
        else:
            validate_popup_size(context, arguments)
            ensure_session(context)
            open_popup(context, arguments)
    except (ScratchError, OSError) as error:
        print(f"tmux-scratch-session: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("tmux-scratch-session: interrupted", file=sys.stderr)
        return 130
    return 0
