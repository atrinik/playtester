# Security policy

Report suspected vulnerabilities through GitHub's private vulnerability
reporting for `atrinik/playtester`. Do not open a public issue for credentials,
authentication bypasses, certificate-validation flaws, unsafe local-control
surfaces, or other exploitable details.

The future playtester must keep credentials, private-server passwords,
certificate material, bot memory, logs, and mutable runtime state outside the
repository and out of issue or CI output. Any operator dashboard must bind only
to loopback, reject cross-origin control, and require an authenticated local
tunnel for remote use.

The game server remains authoritative. Automation must use ordinary player
capabilities, reject stale state, bound work and queues, and clear pending
intent on disconnect or controller loss.
