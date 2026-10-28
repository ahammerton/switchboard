# Security policy

## Supported versions

Security fixes are made for the latest release only. Please check that a problem still happens on the newest version before reporting it.

| Version | Supported |
|---|---|
| 0.1.x | ✅ |

## Reporting a vulnerability

**Please don't open a public issue for security problems.** Report them privately through GitHub:

1. Go to the [Security tab](https://github.com/ahammerton/switchboard/security) of this repository.
2. Choose **Report a vulnerability**. You can also go straight to [the report form](https://github.com/ahammerton/switchboard/security/advisories/new).

Please include:

- the Switchboard version (shown in the startup log and at `/api/meta`)
- how it's run: Docker or Python, and behind a reverse proxy or not
- the steps to reproduce the problem, and what an attacker could do with it

You'll get a reply as soon as possible, usually within a week. Once a fix is released, the advisory is published and you're credited, unless you'd rather not be.

## Scope

Switchboard is meant to run on a **home network**, or behind a reverse proxy that adds its own login. These are in scope:

- getting around the edit token, or changing anything without it
- cross-site request forgery or DNS rebinding that gets past the built-in protections
- reading API keys, the alert webhook URL or the edit token from the server
- cross-site scripting through config, feed or widget content
- making the server read or write files outside its config folder
- crashing the server, or making it unusable, with a single request

These are **by design**, and not vulnerabilities:

- **Anyone who can open the dashboard can view it.** Only changes need the edit token.
- **`EDIT_TOKEN=off` turns protection off.** It's an explicit opt-out.
- **TLS certificates aren't verified** for status checks and widgets, because most lab services use self-signed ones.
- **Secrets are stored unencrypted** in the `config` folder, with file permissions limited to the server's user.
- **Exposing Switchboard directly to the internet isn't supported.** Problems that only arise that way are still welcome as reports, but may be treated as hardening rather than as vulnerabilities.
- **Widgets and Find services make requests** to the addresses configured in them, or entered in Find services. Both need the edit token.

## Security features

Switchboard's built-in protections are described in the README's [Security section](README.md#security):

- the edit token
- cross-site request checks
- DNS rebinding protection
- `ALLOWED_HOSTS`
- how secrets are stored
