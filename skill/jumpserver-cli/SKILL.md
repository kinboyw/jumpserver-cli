---
name: jumpserver-cli
description: Use this skill for authorized access to servers through JumpServer with the local jumpserver-cli wrappers. Do not use it for unauthorized access or stable file transfer.
---

# JumpServer CLI

Use the repository's wrappers for authorized JumpServer access:

```bash
export JMS_BASE_URL='https://jumpserver.example.com'
./jssh <host>
./jexec <host> -- <command>
./jexec --sudo <host> -- <command>
```

Prefer Access Key / Secret authentication. Browser Cookie authentication is
available when the user explicitly chooses it. Never print or store secrets,
temporary passwords, Cookie values, or `--print-command` output.

Auth and temporary Token caches live under `~/.cache/jumpserver-cli` and must
not be read, copied, or shared unless the user explicitly requests a local
debugging operation involving secret material.

## Safety

Before privileged, destructive, service-impacting, or data-mutating commands,
state the exact target and command and request explicit confirmation. The CLI
requires `--yes` for many high-risk operations. Do not add it without that
confirmation.

Use `jssh` for interactive sessions and `jexec` for non-interactive commands.
These commands use a PTY shell; they are not ordinary SSH exec requests.
`jscp` is experimental because some JumpServer gateways reject standard SCP
without a PTY.

If a target search matches multiple assets, use a more specific IP or hostname
and avoid guessing. Do not use direct root login assumptions; the authorized
JumpServer system user controls the remote identity.
