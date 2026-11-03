# Contributor instructions

codex-night wraps the installed Codex CLI. Keep it project-independent, small,
and based on the Python standard library. Preserve Python 3.8+ compatibility.
Interactive recovery is currently qualified on Linux with tmux and Codex CLI 0.166.0.

## Behaviour to preserve

- Keep batch, `--interactive`, and `--watch` modes distinct. `--watch` must never
  start a second Codex process or change the existing session settings.
- Retry only confirmed terminal model-capacity errors. A capacity mention in an
  agent message, source file, or command output must not trigger a retry.
- Preserve the exact session, model, approval policy, and sandbox settings.
  Never switch accounts/models, bypass approval, or change global configuration
  to recover from an error.
- Do not retry authentication, quota, rate-limit, context-limit, or approval errors.
  Do not automatically continue successful or manually interrupted work.
- Respect backoff, Retry-After, retry limits, and persistent submission/cursor state.
  Do not replay a task or submit the same terminal failure twice.
- Before automatic input, verify the bound process identity, active session,
  TTY, foreground ownership, and empty composer. Never send Ctrl+C for recovery.
  Keep the documented tmux input race explicit; do not claim atomic delivery.
- A monitor failure must leave the live TUI running. Explicit owner shutdown must
  clean up its bound native CLI, including CLI processes started through a launcher.
  Never signal a shell, another session, or a reused PID.
- Keep runtime state and logs outside the repository, private to their owner.
  Never commit credentials, transcripts, project data, or machine-specific paths.

## Validation

Use the offline suite from the repository root:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v test_codex_night test_interactive
```

Add regression coverage for changes to retry classification, ownership, signals,
input delivery, or state recovery. Use fake CLI/terminal fixtures; real model
requests or actions in a user's project require explicit authorization.
Obtain independent review for changes to those safety-sensitive paths; keep at
most one independent reviewer active at a time. Report simulated and real-service
verification separately.

## Changes and publication

Read the current implementation and history before editing. Prefer fewer lines
and entities over new abstraction layers. Keep README examples and limitations
consistent with the actual CLI, and preserve the MIT license.
Use concrete English imperative commit subjects; add a useful Russian body when
needed. Keep commits focused. Push, publish releases, or change repository
visibility only when explicitly requested.
