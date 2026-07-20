# Developer Checklist

This checklist must be reviewed before every commit and pull request.
It ensures consistent quality and prevents common issues.

## Before You Start

- [ ] Pull the latest `main` branch
- [ ] Create a properly named branch (`feat/`, `fix/`, `docs/`, etc.)
- [ ] Open an issue for non-trivial changes to align on the approach

## Before Every Commit

- [ ] Code compiles/builds without errors
- [ ] Linter passes (backend: `cd backend && ruff check .`; frontend: `cd frontend && npm run lint`)
- [ ] Formatter applied (backend: `cd backend && black .`)
- [ ] No `console.log`, `print()`, or debug statements in production code
- [ ] No commented-out code blocks
- [ ] No hardcoded secrets, API keys, or credentials
- [ ] No `any` types in TypeScript (without justification)
- [ ] No unused imports or variables
- [ ] No trailing whitespace
- [ ] Files end with a newline
- [ ] Commit message follows Conventional Commits format
- [ ] One logical change per commit

## Before Every Pull Request

- [ ] All CI checks are green
- [ ] All existing tests pass
- [ ] New tests added for new functionality
- [ ] PR description filled out completely
- [ ] Related issue referenced (`Closes #N`)
- [ ] PR is under 400 lines of diff (split if larger)
- [ ] No merge conflicts with `main`
- [ ] Branch is up to date with `main`
- [ ] Self-reviewed your own code
- [ ] Documentation updated if needed

## Open Issues and PRs

Check regularly and address:

- [ ] Any open issues assigned to you
- [ ] Any PRs awaiting your review
- [ ] Any failing CI pipelines
- [ ] Any dependency updates to review and apply manually (tracked in `requirements.txt` / `package.json`)
- [ ] Any stale branches that should be deleted

## Redundant File Check

Before pushing, verify no junk files are included:

- [ ] No `.DS_Store`, `Thumbs.db`, or OS-specific files
- [ ] No `node_modules/`, `__pycache__/`, or build output
- [ ] No `.env` files (only `.env.example`)
- [ ] No `*.log`, `*.bak`, `*.orig`, `*.swp` files
- [ ] No large binary files unless necessary
- [ ] No IDE-specific config files (`.idea/`, `.vscode/` should be gitignored)

## Update Check

Periodically verify:

- [ ] Dependencies are up to date
- [ ] Security vulnerabilities addressed
- [ ] README and documentation reflect current state
- [ ] CHANGELOG updated for releases
- [ ] License information is current

## Warning and Alert Check

Before merging:

- [ ] No new TypeScript/ESLint warnings introduced
- [ ] No new Python linter (ruff/flake8) warnings
- [ ] No deprecation warnings from dependencies
- [ ] No security scan warnings (CodeQL, gitleaks)
- [ ] CI pipeline is fully green (not just passing with warnings)

## Codebase Cleanup Check

Perform these checks quarterly or before major releases:

### Version Consistency
- [ ] `VERSION` file matches latest release in `CHANGELOG.md`
- [ ] `backend/main.py` FastAPI version matches `VERSION`
- [ ] `frontend/package.json` version matches `VERSION`
- [ ] All version references updated after release

### License Consistency
- [ ] `LICENSE` file matches actual license used
- [ ] `frontend/package.json` `license` field matches `LICENSE` file
- [ ] README badges reflect correct license
- [ ] All source file headers (if present) match license

### Documentation Hygiene
- [ ] All internal documentation links resolve (no broken references)
- [ ] README "Documentation Index" section lists only existing files
- [ ] Remove references to deleted or archived documents
- [ ] Update cross-references after document reorganization

### Configuration Cleanup
- [ ] `.editorconfig` contains only languages/frameworks actually used
- [ ] Remove obsolete configuration files (e.g., old `ruff.toml` after migration to `pyproject.toml`)
- [ ] Verify `.gitignore` patterns are current and comprehensive

### Obsolete Content Removal
- [ ] Delete archived/planning documents marked as "已存档" or "Archive"
- [ ] Remove deprecated feature documentation
- [ ] Clean up duplicate `.env.example` files (should be one per service)
- [ ] Remove test stubs or placeholder files no longer needed

### Dependency Audit
- [ ] Review `requirements.txt` for unused dependencies
- [ ] Review `package.json` for unused npm packages
- [ ] Verify Docker images use current base versions
- [ ] Check for duplicate version files across services
