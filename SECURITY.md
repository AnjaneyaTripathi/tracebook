# Security policy

## Supported versions

Only the latest release on the default branch receives security fixes. The current
line is 0.1.x.

## Reporting a vulnerability

Please do not open a public issue for security problems. Report them privately
through GitHub's **Security → Report a vulnerability** form on this repository.
Include the Tracebook version or commit, your OS and agent versions, and the
steps needed to reproduce the problem. Remove real prompts, credentials and
personal paths from anything you attach.

Expect an acknowledgement and a triage decision as the maintainer's time allows.
This is a personal project, so no response-time guarantee is made.

## Scope

In scope:

- Anything that makes Tracebook send data off the machine without the user enabling
  a delivery channel.
- Remote access to the dashboard or OTLP receiver, including Host/Origin or
  DNS-rebinding bypasses of the localhost checks.
- Hook or installer behavior that runs unintended commands, follows symlinks
  outside its target, or corrupts agent configuration.
- Parser or import input that leads to code execution, path traversal or
  unbounded resource use.

Out of scope or already documented:

- Other processes running as the same user can read `~/.tracebook`. This is stated
  in [the operations guide](docs/OPERATIONS.md).
- Pattern-based redaction misses secrets it does not recognize. These are welcome as
  regular issues, with a sanitized example. Add your own patterns with
  `redact_patterns`, or set `capture_content = false`.
