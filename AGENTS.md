# Agent Instructions

This project uses **bd** (beads) for issue tracking. Run `bd onboard` to get started.

## Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --status in_progress  # Claim work
bd close <id>         # Complete work
bd sync               # Sync with git
```

## Landing the Plane (Session Completion)

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   bd sync
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds

## alarm.com Browser Engine Rules

### NEVER use page.goto() after login
alarm.com is an **Ember SPA**. The `/web/*` routes (e.g. `/web/video`, `/web/dashboard`) are **client-side only** — they don't exist as server-side routes. Hitting them with `page.goto()` destroys the SPA and returns "Page Not Found".

- `page.goto()` is ONLY safe for the initial login (`/login` is a real server route)
- All post-login navigation MUST use SPA nav link clicks (e.g. `a[data-testid="video-link"]`)
- Parking/unparking the browser must click nav links, never `page.goto()`

### SPA nav selectors
- **Video page**: `a[data-testid="video-link"]`
- **Home page**: `a[data-testid="home-link"]`
- **General**: `a[data-testid]` lists all SPA nav links

### Version bumps require updating 6 files
When bumping versions, ALL of these must be updated:
1. `alarmdotcom_cameras/config.yaml`
2. `alarmdotcom_cameras/CHANGELOG.md`
3. `alarmdotcom_cameras/rootfs/usr/share/alarmdotcom_cameras/routes.py`
4. `alarmdotcom_cameras/rootfs/usr/share/alarmdotcom_cameras/static/index.html`
5. `alarmdotcom_cameras/custom_components/alarmdotcom_cameras/manifest.json`
6. `custom_components/alarmdotcom_cameras/manifest.json`

