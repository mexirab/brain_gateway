# Suite theme

One palette, font and corner radius for every app in the stack, so the
dashboard, the launcher, Home Assistant and Grist feel like one product.

```
theme/tokens.json  ──python3 theme/build.py──▶  frontend/src/styles/tokens.css    (dashboard)
                                                frontend/src/styles/theme-meta.ts (theme-color, manifest)
                                                theme/dist/homepage.css           (gethomepage)
                                                theme/dist/home-assistant.yaml    (Home Assistant)
                                                theme/dist/grist.css              (Grist)
```

- **Edit `tokens.json` only**, run `python3 theme/build.py` and commit the
  regenerated files. CI (`Suite Theme In Sync`) fails if they drift.
- `dark` is the original Jess palette and `light` is its daytime pair. In
  `light`, every text, status and accent color clears WCAG AA (4.5:1) on every
  surface and on its own 10–20% badge tint. Re-check contrast if you change one.
- The font is Inter, falling back to the platform UI font (SF on Apple
  devices). The dashboard bundles Inter. Other apps use it only if the device
  has it installed, otherwise they get the system font.

## Applying it

### Jess dashboard
This is automatic: the build imports `tokens.css`. Each device picks Dark,
Light or System in **Settings → Appearance**, stored in a `jess-theme` cookie.
Dark is the default.

On iPhone, the status-bar style is read when the app is added to the Home
Screen. After switching between Dark and Light/System, remove the app and add
it again.

### Homepage (gethomepage)
```bash
cp theme/dist/homepage.css <homepage>/config/custom.css
```
Reload the page; Homepage serves `custom.css` live. Keep `theme: dark` (or
light) in `settings.yaml`. The `color:` setting no longer matters, because the
custom CSS replaces its palette.

### Home Assistant
1. `configuration.yaml` must have the themes include indented **under**
   `frontend:`:
   ```yaml
   frontend:
     themes: !include_dir_merge_named themes
   ```
2. Copy the theme:
   ```bash
   mkdir -p <ha config>/themes
   cp theme/dist/home-assistant.yaml <ha config>/themes/jess_suite.yaml
   ```
3. Restart HA if you changed `configuration.yaml`. Otherwise run the
   `frontend.reload_themes` action.
4. In your HA **profile**, set Theme to **Jess Suite** and the mode to
   **Auto**. The theme has both light and dark modes.

### Grist
`grist/docker-compose.yml` mounts `theme/dist/grist.css` as
`/grist/static/custom.css` and sets `APP_STATIC_INCLUDE_CUSTOM_CSS=true`. After
a theme change:
```bash
cd grist && docker compose up -d --force-recreate
```
The recreate is needed because a single-file bind mount keeps the old inode.

The theme covers both Grist Light and Grist Dark (Grist's own appearance
setting chooses between them).

### Apps with a built-in setting only
These have no custom-CSS hook. Set them by hand to get close:

| App | Setting |
|---|---|
| Paperless-ngx | Settings → Appearance: dark mode on, theme color `#6f63ff`. Set the app title to something short ("Documents"). |
| Actual Budget | Settings → Theme: **Midnight** (closest to Jess dark), or Light. |
| Grafana | Profile → Preferences → Theme: Dark. Custom branding is Enterprise-only. |

### Not themed yet
- **Audiobookshelf, Immich and SearXNG** have no supported way to add CSS.
  Theming them would need a small proxy that injects a stylesheet, which
  breaks easily when those apps update. That is deliberately not done.
- The native mobile apps (Immich, Audiobookshelf, HA Companion) keep their own
  look. HA Companion does follow the HA profile theme.
