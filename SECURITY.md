# Security Policy

## Sensitive Files & Credentials

The configuration file `skills/argon-coding-bridge/config/accounts_pool.json` holds live Google account credentials used for automated browser login.

- This file is strictly **git-ignored** (`.gitignore`), never tracked by version control, and never packed into `argon_context.zip`.
- Each local installation maintains its own independent copy of credentials.
- `package_context.py` enforces automated pattern-based secret stripping on all outgoing archives.
- Automated secret-guard CI checks reject any commit or pull request attempting to track credentials.

## Prohibited Content in Notes & Issues

- Never paste passwords, private tokens, API keys, or session credentials into GitHub Issues, Pull Requests, discussion threads, or fix notes.
- When recording a fix with `record_fix.py`, any attempt to include credentials, auth tokens, or private keys is automatically rejected by pattern scanners.

## Reporting a Security Vulnerability

If you discover a security issue or credential vulnerability in Argon Coding Bridge, please do NOT file a public issue.

Instead:
1. Contact the project owner directly via email or private message.
2. Provide a clear description of the potential vulnerability, affected components, and steps to reproduce.
3. Allow the project maintainers reasonable time to address the issue before any public disclosure.
