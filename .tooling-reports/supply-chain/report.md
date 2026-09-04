# Supply Chain Risk Report — `automated-invoicing-system`

**Scanned:** `.`  
**Commit:** `d43d5ea7892f`  
**Manifests read:** `pyproject.toml`  
**Scanned at:** 2026-08-29T10:29:50+00:00  
**Direct dependencies:** 12 (PyPI 12)

## Summary

- **No known advisory affects any of the 12 direct dependencies** — but 12 of them were not checked at a project-resolved version (latest-release fallback or go.mod minimum; see Method and caveats).
- Transitive dependencies were **not examined**: no lockfile resolves the transitive tree (package-lock.json, uv.lock, or a go 1.17+ go.mod).
- 0 of 12 dependencies carry at least one finding, 0 of which reach production.
- Weakest coverage: **Install-time script execution**, established for 0 of 12; the Coverage section lists every criterion.

## Production dependencies

7 dependencies are declared as runtime dependencies and ship in the built artifact. Advisory status is given for every one, clean or not.

| Dependency | Version | Advisories | Other findings |
|---|---|---|---|
| `google-api-python-client` | 2.199.0 (latest, not the project's pin) | none known | — |
| `google-auth-oauthlib` | 1.4.1 (latest, not the project's pin) | none known | — |
| `jinja2` | 3.1.6 (latest, not the project's pin) | none known | — |
| `openpyxl` | 3.1.5 (latest, not the project's pin) | none known | — |
| `pydantic` | 2.13.5 (latest, not the project's pin) | none known | — |
| `python-dotenv` | 1.2.3 (latest, not the project's pin) | none known | — |
| `typer` | 0.27.2 (latest, not the project's pin) | none known | — |

## Findings

No dependency was flagged on these criteria.

## Upstream repository and CI hygiene — OpenSSF Scorecard

These criteria describe each dependency's own repository, not the audited
project. Remediation, where any exists, is upstream.

No criterion in this tier was assessable for any dependency.

## Transitive advisories

Not examined: no lockfile resolves the transitive tree (package-lock.json, uv.lock, or a go 1.17+ go.mod). Commit a lockfile to close this gap.

## Informational

Measured, not flagged.

- **Publish provenance**: not determinable for any dependency in this project.
- **Security policy published**: 11 of 11 publish a security policy.
- **Download volume**: not determinable for any dependency in this project.

## Coverage

What was and was not measured, per criterion.

| Criterion | Tier | Assessed | Flagged | Not assessable |
|---|---|---|---|---|
| Known advisories | A | 12/12 | 0 | 0 |
| Deprecated or yanked | A | 12/12 | 0 | 0 |
| Repository archived | A | 11/12 | 0 | 1 |
| Maintenance activity | A | 11/12 | 0 | 1 |
| Publisher concentration | B | 0/12 | 0 | 12 |
| Install-time script execution | B | 0/12 | 0 | 12 |
| Dangerous CI workflow | scorecard | 0/12 | 0 | 12 |
| CI token permissions | scorecard | 0/12 | 0 | 12 |
| Checked-in binaries | scorecard | 0/12 | 0 | 12 |
| Changes reviewed by a second person | scorecard | 0/12 | 0 | 12 |
| Publish provenance | info | 0/12 | 0 | 12 |
| Security policy published | info | 11/12 | 0 | 1 |
| Download volume | info | 0/12 | 0 | 12 |

## Not assessable

**Repository archived**

- 1 dependency — no source repository could be resolved for this package: `openpyxl`

**Checked-in binaries**

- 11 dependencies — the Scorecard API did not answer: `google-api-python-client`, `google-auth-oauthlib`, `jinja2`, `mypy`, `pillow`, `pydantic` and 5 more
- 1 dependency — no source repository to score: `openpyxl`

**Changes reviewed by a second person**

- 11 dependencies — the Scorecard API did not answer: `google-api-python-client`, `google-auth-oauthlib`, `jinja2`, `mypy`, `pillow`, `pydantic` and 5 more
- 1 dependency — no source repository to score: `openpyxl`

**Dangerous CI workflow**

- 11 dependencies — the Scorecard API did not answer: `google-api-python-client`, `google-auth-oauthlib`, `jinja2`, `mypy`, `pillow`, `pydantic` and 5 more
- 1 dependency — no source repository to score: `openpyxl`

**Download volume**

- 12 dependencies — PyPI's download counters are disabled and return -1: `google-api-python-client`, `google-auth-oauthlib`, `jinja2`, `mypy`, `openpyxl`, `pillow` and 6 more

**Install-time script execution**

- 12 dependencies — install-time execution depends on whether a wheel or an sdist is installed, which this collector does not determine: `google-api-python-client`, `google-auth-oauthlib`, `jinja2`, `mypy`, `openpyxl`, `pillow` and 6 more

**Publish provenance**

- 12 dependencies — PyPI publish attestations are not read by this collector: `google-api-python-client`, `google-auth-oauthlib`, `jinja2`, `mypy`, `openpyxl`, `pillow` and 6 more

**Publisher concentration**

- 12 dependencies — PyPI publishes no upload ACL, so who can publish this package is not observable: `google-api-python-client`, `google-auth-oauthlib`, `jinja2`, `mypy`, `openpyxl`, `pillow` and 6 more

**Security policy published**

- 1 dependency — no source repository could be resolved for this package: `openpyxl`

**Maintenance activity**

- 1 dependency — no source repository could be resolved for this package: `openpyxl`

**CI token permissions**

- 11 dependencies — the Scorecard API did not answer: `google-api-python-client`, `google-auth-oauthlib`, `jinja2`, `mypy`, `pillow`, `pydantic` and 5 more
- 1 dependency — no source repository to score: `openpyxl`

## Method and caveats

- cache directory ownership was not verified: os.getuid is POSIX-only, so a cache at C:\Users\thoma\AppData\Local\Temp\supply-chain-risk-auditor-cache owned by another user would not be detected on this platform. Keep the cache under your own user profile, or pass --cache with a private path.
- 12 dependencies are specified as a version range with no lockfile, so their advisories were matched against the current latest release rather than against what this project installs — commit a lockfile for an exact answer: google-api-python-client, google-auth-oauthlib, jinja2, mypy, openpyxl, pillow, pydantic, pytest ...
- Optional tooling detected: npm. Not installed, so not used: bundler-audit, cargo-audit, osv-scanner, pip-audit.
- Direct dependencies only. Advisories attach to the package that ships the affected code, so an umbrella package can look clean while its components are not — rails 5.0.0 reports 0 advisories where actionpack 5.0.0 reports 10. Transitive dependencies were not examined: no lockfile resolves the transitive tree (package-lock.json, uv.lock, or a go 1.17+ go.mod).
- HTTP sources: 15 fetched, 45 served from cache (oldest 0.1h old), 0 refetched as stale, 0 unavailable offline, 11 errors.
