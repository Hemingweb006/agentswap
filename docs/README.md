# Images used by the README

| file | what it is | notes |
|:--|:--|:--|
| `banner.png` | the juggler logo + AGENTSWAP wordmark | **solid black background, not transparent** |
| `demo.png` | terminal screenshot of `run` carrying a session | crop out your email address |
| `social.png` | optional, 1280x640 GitHub social preview | Settings → General → Social preview |

## Why the banner background matters

The wordmark is white outlined text. If the PNG is exported with a transparent
background it will be invisible against GitHub's light theme — the most common
way a good banner silently breaks. Flatten it onto solid black before committing.

## Before committing `demo.png`

The Antigravity header prints the signed-in account. Crop above it or blur that
line; a README screenshot is permanent and indexed.

Good frames to capture, in order of value:

1. The full `run` flow: `WALL` → `CARRIED 55.6KB ──▶ 957B  59x smaller` → the
   target launching with carried context. This one screenshot is the entire pitch.
2. `agentswap agents` showing three green dots.
3. The banner on `run` startup.
