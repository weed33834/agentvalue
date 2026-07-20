# Security Policy

## Supported Versions

Only the latest release receives security updates.

## Reporting a Vulnerability

If you discover a security vulnerability, **do not open a public issue**.

Instead, please report it privately:

1. Email: **badhope@noreply.gitcode.com**
2. Or use GitHub's [Security Advisories](https://gitcode.com/badhope) feature

Include the following in your report:

- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if any)

You will receive a response within 72 hours. Please allow up to 30 days for a
fix before public disclosure.

## Security Measures

This project follows these security practices:

- All changes require Pull Request review before merging
- CI pipeline includes security scanning (Trivy filesystem scan, pip-audit dependency vulnerabilities, gitleaks secrets scanning)
- No secrets or credentials are committed to the repository
- Dependencies are reviewed and updated manually by maintainers (see `backend/requirements.txt` and `frontend/package.json`)

## Disclaimer

This software is provided "as is" without warranty. The maintainer is not
liable for any damages arising from the use of this software.
