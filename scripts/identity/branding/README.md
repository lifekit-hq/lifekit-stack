# Sign-in page branding

Marks and colors for Logto's sign-in page, applied by
`scripts/identity/apply-branding.sh` (`docs/runbook.md` "Sign-in page branding").

| App | Seed (light primary) | Dark primary | Mark |
| --- | --- | --- | --- |
| lk (lifekit dashboard, devclaw console via the gate) | Indigo `#4f46e5` | `#8e9aff` | `lk.svg` |
| fs (Finance Sentry) | Petrol `#175a6d` | `#71afc4` | `fs.svg` |

The marks are `markSvg({app, tile: <seed>, ink: '#ffffff'})` from
`@lifekit-hq/tokens` (`projects/tokens/brand/mark.mjs`, lifekit-common at
`26b5f00`), the same mark the apps' favicons use. The seeds are the owner's
2026-10-07 lk-theme-direction pick (quiet, per-app seed); the dark primaries
are the seed engine's derived dark accents. To refresh a mark, re-render it with
that function and commit the SVG; the script inlines it as a `data:` URI, so
nothing is hosted.
