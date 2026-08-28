# Security Policy

## Supported version

Security and click-safety fixes are applied to the latest release on `main`.

## Reporting a vulnerability

Use GitHub's private security-advisory feature for vulnerabilities that could
cause unintended input, target-window confusion, command execution, or bundled
binary tampering. Do not publish exploit details in a public issue before a fix
is available.

For ordinary recognition errors that cannot trigger a click, open a regular
issue with a redacted screenshot and the exact Windows/Python versions.

## Safety boundary

- Mouse input starts disabled and must remain an explicit opt-in.
- A detector or engine error must stop the action; it must not guess a move.
- Tests and CI must never interact with a real desktop game window.
- Release changes to binaries or weights must include upstream provenance,
  license terms, and SHA-256 hashes.
