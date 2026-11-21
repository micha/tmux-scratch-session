# tmux-scratch-session

Toggle a persistent scratch-session popup, from a shell inside tmux:

```sh
tmux-scratch-session
```

The scratch session is always named **`zzzzzzzz`**. It is shared by all sessions
and clients on the current tmux server. Outside that session, the command opens
the popup. Inside it, the same command detaches only the selected client, closing
its popup without stopping the scratch shell or other processes. Run it again
from the outer session to return.

This repository owns the command, not your personal tmux configuration. Installing
it does not register a key binding or change any configuration files.

## Install

Requires **Python 3.10+**, **tmux 3.8+**, and a POSIX system (Linux or macOS).
The Python package has no third-party runtime dependencies. The tmux requirement
matches the popup behavior and theme-aware style defaults used by this command.

From this checkout, choose either installation method:

### Nix

```sh
nix profile add .
```

The flake installs the Python console command and provides tmux as a fallback.
A tmux executable already on your `PATH` takes precedence. Packages are exposed
for x86-64 Linux, ARM64 Linux and ARM64 macOS; the pinned Nixpkgs no longer
supports Intel macOS. Flakes must be enabled in your Nix installation.

To inspect the command without installing it:

```sh
nix run . -- --help
```

### pipx

Install [pipx](https://pipx.pypa.io/stable/installation/) and tmux separately, then:

```sh
pipx install .
```

Ensure pipx's binary directory is on your `PATH` (`pipx ensurepath` can help).
Both methods expose the same `tmux-scratch-session` command.

## Usage

```sh
# Outside zzzzzzzz: open an 80% wide, 80% high popup.
# Inside zzzzzzzz: detach this client, leaving the scratch session running.
tmux-scratch-session

# Use percentage dimensions or absolute cell counts.
tmux-scratch-session --width 70% --height 25

# Override native tmux style strings.
tmux-scratch-session --style default --border-style default

# Select an exact client when more than one could own the popup.
tmux list-clients -F '#{client_name}'
tmux-scratch-session --client /dev/pts/5

# Use another tmux executable.
tmux-scratch-session --tmux /path/to/tmux
```

Default styles match a terminal-background popup with a theme-aware border:
`bg=terminal` and `fg=themelightgrey,bg=terminal`. Dimensions must be at least
three cells, or percentages from `1%` to `100%`. The resulting size must fit the
selected client's terminal; this is checked before creating a session. tmux still
handles final geometry and style interpretation, including terminal resizes that
happen after the check. Size and style settings are not applied when detaching;
arguments must still pass normal CLI parsing.

### Client and server selection

The command reads `$TMUX` and explicitly targets that server's socket. It does
not fall back to the default server or start a replacement server if yours has
stopped. Socket paths containing commas are supported.

It considers attached terminal clients in the calling session. With several
candidates, a single client whose active pane matches `$TMUX_PANE` wins;
otherwise the command refuses to guess and requires `--client`. An explicit
client must be an exact attached client name in the calling session. Control-mode
clients are not selected for either opening or detaching.

When integrating the command with a tmux key binding, pass the invoking
`#{client_name}` as `--client` to avoid client-selection ambiguity. No binding
is installed by this repository.

Outside `zzzzzzzz`, the command is intended for ordinary tmux panes, not nesting
arbitrary popups. Inside `zzzzzzzz`, it detaches the selected client instead of
opening another popup. Ambiguous client selection still requires `--client`;
other attached scratch clients are left alone. If used in a terminal directly
attached to the scratch session, it detaches that terminal client too.

### Session lifecycle

- The name `zzzzzzzz` is hardcoded; there is no session-name option.
- First use creates a **detached** session using your normal tmux default shell
  or command, starting in the calling pane's working directory.
- Later uses reuse that exact session without replacing its windows, panes,
  processes or working directories. An existing session with this reserved name
  is treated as the scratch session; do not use the name for unrelated work.
- Opening the popup sets the scratch session's local status option to `off`.
  The outer client's session is not switched, and its status setting is not changed.
  Detaching does not change options, validate popup geometry or create a session.
- A nested tmux client attaches inside the popup. `$TMUX` is removed only from
  this attachment process; the original socket is passed explicitly.
- Detaching the nested client or closing the popup leaves the session running.
  Exiting its last shell/window may destroy the session; next use recreates it.
- Two popups opened on different clients attach to the **same** scratch session.
  They share its state and input, rather than providing independent workspaces.

The launcher waits for the popup to close. Ordinary detach and popup dismissal
are successful exits. A closed popup's nested attachment may exit via `SIGHUP`;
this is treated as normal dismissal rather than an error.

### Hooks, workmux and failure handling

Creating the scratch session runs normal tmux session-creation hooks. Keep any
workmux exclusion for the exact name `zzzzzzzz` in your own configuration; this
command neither installs hooks nor disables plugins on your behalf.

The command does not edit configuration files, key tables or global options,
and does not kill sessions. It only creates/reuses the reserved session, sets
its local status option and opens the popup, or detaches the selected scratch
client when already inside. If opening the popup fails after
creation, the scratch session is deliberately left running rather than destroyed.
Creation races with another client are handled by rechecking the exact name.

Errors are reported on stderr with a nonzero exit status. Argument errors exit
with status 2; a launcher keyboard interruption exits with status 130. The tool
has no persistent state files or recovery snapshots: your state lives in tmux.

## Development

With Python and tmux on `PATH`:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Or use the supplied Nix tools and checks:

```sh
nix develop
nix flake check
nix build
```

Integration tests use temporary homes, private tmux servers and real attached
clients on pseudo-terminals. They exercise popup creation, invocation-based
detach, reuse, client selection and failure handling without reading your real config or
modifying your live server. Missing-tmux skips are not complete validation.
The Nix package provides tmux and tests its installed entry point during build.
Only platforms where these tests have actually run should be considered tested.

See [AGENTS.md](AGENTS.md) for maintenance guidance and the
[tmux manual](https://github.com/tmux/tmux/blob/master/tmux.1) for native command semantics.
