# Agent guide

## Scope and ownership

This repository contains a standalone Python CLI, not a personal tmux config.
[README.md](README.md) owns user-facing behavior, installation instructions and
lifecycle limitations. This file owns agent maintenance guidance.
`pyproject.toml` owns package metadata and the console entry point; `flake.nix`
packages that same Python application for Nix.

Do not edit the user's tmux config, install key bindings, operate on their live
server, modify their real pipx/Nix profiles, commit, add remotes or publish the
repository without an explicit request. All validation must use isolated resources.

## Layout

- `src/tmux_scratch_session/cli.py`: argument parsing, client selection, session
  management and popup attachment.
- `src/tmux_scratch_session/__main__.py`: `python -m tmux_scratch_session` entry point.
- `tests/test_cli.py`: standard-library unit tests and real tmux/PTY integration tests.
- `pyproject.toml`: pip/pipx packaging.
- `flake.nix` and `flake.lock`: reproducible Nix packages, checks and development shell.

## Implementation rules

- Support Python 3.10+ and tmux 3.8+, with standard-library Python runtime dependencies only.
- Keep `zzzzzzzz` hardcoded and target it exactly. Do not add session-name customization
  or silently rename it: personal session-creation hooks may depend on this name.
- Keep functions focused, use explicit types at boundaries, and prefer actionable errors.
- Use four-space indentation and keep lines near 100 characters.
- Invoke subprocesses using argument arrays, never `shell=True`.
- tmux argv handling has its own semicolon escaping rules, separate from shell quoting.
- Explicitly address the current socket, and do not automatically start a missing live server.
- Preserve calling-client context; reject ambiguous targets instead of selecting a random client.
- Create the session detached. Do not use `new-session -A` as a shortcut: reusing an
  existing session must not switch or detach the outer client.
- Handle simultaneous first-use creation without hiding genuine errors.
- Inside `zzzzzzzz`, detach only the selected client with `detach-client -t`;
  do not open another popup, create a session or change options. Preserve ambiguity
  checks, and never use `-s`, `-a` or `-P` to detach other clients or signal parents.
- Reuse existing session state. Never kill sessions as startup-error cleanup.
- Clear `$TMUX` only in the nested attachment process, and keep the explicit socket there.
- Keep the Python attachment trampoline tied to the same interpreter as the installed CLI.
- Limit automatic changes to the reserved session's status option, popup creation
  and selected-client detachment. Do not install hooks, change global options or
  reconfigure workmux.
- Keep CLI help, README and tests aligned when behavior changes.

## Validation

Run from the repository root:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
nix flake check
nix build
```

All integration tests must use private sockets, temporary homes and synthetic
configuration. Never source the user's real config or kill a user's server.
Tests attach clients to real pseudo-terminals; track and clean up server, client,
launcher and file-descriptor resources even after failures. Do not replace real
popup tests solely with subprocess mocks. Missing-tmux skips are not full validation.

Cover creation, reuse, invocation-based detach that preserves other clients and
session state, persistent state after detach/close, exact session targets,
creation races, multiple clients and servers, socket/path quoting, invalid context
and arguments, and launch failures that leave scratch state intact.

For packaging changes, test both installation paths using temporary profiles:
`PIPX_HOME`/`PIPX_BIN_DIR`/`PIPX_MAN_DIR` for pipx and `nix profile add --profile
<temporary-profile> .` for Nix. Set `TSS_TEST_EXECUTABLE` to the installed command's
absolute path to exercise its real entry point in the integration suite. No real
user profiles should change during testing.

Nix local Git flakes ignore untracked files, so add files to the index before
validation. Report validation commands, skips and platform limitations honestly.
