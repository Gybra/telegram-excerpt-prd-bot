# Security Policy

## Supported versions

| Version | Supported |
| ------- | --------- |
| 0.1.x   | Yes       |

Only the latest release on `main` receives security fixes.

## Reporting a vulnerability

**Do not open a public issue.** Instead, email the maintainer directly
at the address listed in the Git commit history, or use
[GitHub's private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
on this repository.

Please include:

1. A description of the vulnerability and its impact.
2. Steps to reproduce (a minimal PoC is ideal).
3. The version or commit hash you tested against.

You can expect an initial response within **72 hours**. If the issue is
confirmed, a fix will be released as soon as possible (usually within a
week) and you will be credited in the changelog unless you prefer to
remain anonymous.

## Security model

- **Webhook endpoints** are authenticated via
  `X-Telegram-Bot-Api-Secret-Token` (per-bot random secret).
- **Internal endpoints** (`/tasks/process`, `/admin/setup`) require a
  bearer token (`SCHEDULER_AUTH_TOKEN`).
- **Secret comparisons** use `hmac.compare_digest` to prevent timing
  attacks.
- **Admin commands** are restricted to the single `FORWARD_CHAT_ID`.
- **Tokens and API keys** are stored as `SecretStr` and never appear in
  logs or `repr()` output.
- **Dependencies** are audited on every CI run via `pip-audit`.

## Dependency updates

Dependabot is enabled for both pip and GitHub Actions. Security patches
for transitive dependencies (e.g. Starlette CVEs pulled in via FastAPI)
are pinned explicitly in both `pyproject.toml` and `requirements.txt`.
