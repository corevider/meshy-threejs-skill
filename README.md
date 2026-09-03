# Meshy 3D skill for Claude Code

An agent skill that drives the [Meshy](https://meshy.ai) API end to end for browser games:
text/image/multi-image to 3D, retexture, remesh, humanoid auto-rig, animation from a
678-clip mocap library or a text prompt, plus GLB quality checks and a three.js viewer for
verifying the result in a real browser.

Written for [Claude Code](https://claude.ai/code) skills (`~/.claude/skills/`), but the
`scripts/` are plain Python 3 with no third-party imports - they work as a standalone CLI in
any pipeline.

## Install

```bash
git clone https://github.com/corevider/meshy-threejs-skill.git \
  ~/.claude/skills/threejs-3d-generator-meshy      # Codex: ~/.codex/skills/...
export MESHY_API_KEY=msy_...                       # get one at meshy.ai
```

Put the export **above** the non-interactive guard in `~/.bashrc` (the `case $- in *i*)`
block), or only interactive shells will see it - a plain `bash -c` then reports a missing
key. The tooling tells you this when it happens.

Optional: `pip install pillow` (only for `slim_glb.py`'s texture downscaling).

## Quickstart

```bash
S=~/.claude/skills/threejs-3d-generator-meshy/scripts/meshy_3d_asset.py

python3 $S balance
python3 $S text --prompt "game-ready sci-fi hover bike, sleek armored panels, PBR" \
  --target-polycount 20000 --texture-resolution 2k --pbr \
  --wait --download --out-dir assets/models/bike

python3 $S list-animations --search sword
python3 $S character-pipeline \
  --prompt "stylized cyber runner character, game-ready outfit" \
  --pose-mode t-pose --animations idle,walk,run \
  --out-dir assets/models/runner
```

`character-pipeline` runs generate -> texture -> rig -> clips, validates the skeleton and
each clip, and recovers automatically when the mesh is too dense to rig.

## Commands

| command | what it does |
| --- | --- |
| `text` / `refine` | text to 3D; the preview and texture passes, chained |
| `image` / `multi-image` | one reference image, or 1-4 views of the same subject |
| `retexture` / `remesh` | restyle textures; retopologize, decimate, convert |
| `rig` / `animate` / `motion` | humanoid auto-rig; library clip or generated motion |
| `character-pipeline` | the whole chain with QA and face-limit recovery |
| `list-animations` | search the bundled 678-clip library by name or category |
| `status` / `download` / `balance` | task state, asset download, remaining credits |
| `validate-rig` / `validate-animation` | GLB QA: skeleton symmetry, warp signatures |

`scripts/slim_glb.py` shrinks output for previews: clip GLBs lose their duplicated mesh
(8.1 MB -> ~50 KB) and rig textures are re-encoded or split out.

## Verify in the browser

`validate-*` reads the files; the viewer plays them on the actual skeleton.

```bash
cp ~/.claude/skills/threejs-3d-generator-meshy/viewer/clip-viewer.html preview/
cd preview && python3 -m http.server 8322
# http://localhost:8322/clip-viewer.html?rig=rig.glb&clips=idle.glb,walk.glb,run.glb
```

It reports per clip: track counts, unresolved bone targets, hips/foot travel and head
height, and exposes `window.__report` / `window.__pose(i, seconds)` for headless probes. The
two import fixes below are toggles, so you can see the raw failure each one prevents.

## Gotchas this encodes (all measured, not guessed)

- **`should_remesh` must be explicit.** `target_polycount` and `topology` are ignored
  without it, and its default varies by model version. A 20k request returned **1,936,276
  faces / 74 MB**. The CLI now sets the flag whenever a polycount or topology is given.
- **Rigging caps at 320,000 faces** (the docs say 300,000) and rejects the task at submit
  time, for free. Its error message points at `POST /openapi/v2/remesh`, which **404s** -
  the live path is `/openapi/v1/remesh`.
- **Humanoid rigs only.** Quadrupeds, birds and vehicles are rejected outright; use a
  provider with creature rigs for those.
- **Clips can carry a constant off-rest scale.** The `Idle` clip (action 0) ships `Hips`
  scale `1.176`, rendering that clip's character 17.6 % taller than its neighbours - drop
  constant scale tracks at import.
- **Clips are baked with root motion.** Zero the horizontal root translation, keep the
  vertical component (jump arcs and gait bob live there).
- **A failed texture renders a white silhouette**, not an untextured model: the material's
  `emissiveFactor` is `[1,1,1]` driven by its texture, and GLB images decode through a
  `blob:` URL that a strict CSP can block. `references/threejs-integration.md` has both
  fixes.

## Meshy or Tripo?

Meshy wins on humanoid animation coverage (678 mocap clips plus text-to-motion), explicit
`pose_mode`, multi-view retexturing, and one-call remesh. Tripo wins on non-humanoid rigs
(quadruped, avian, serpentine, aquatic) and stylization (lego, voxel, minecraft). Both do
text/image to 3D, quad topology and PBR.

## Layout

```
SKILL.md                        agent instructions (the skill entry point)
scripts/meshy_3d_asset.py       the API client / CLI
scripts/slim_glb.py             GLB slimming for previews and embedding
scripts/scrape_animation_library.py   rebuild the animation table from Meshy's docs
references/api-notes.md         endpoints, parameters, limits, measured behaviour
references/threejs-integration.md   loading rigs and clips in three.js
references/animations.csv       678 action ids with names and categories
viewer/clip-viewer.html         three.js viewer for rig + clip verification
```

MIT licensed. See `NOTICE.md` for the animation table's provenance. Not affiliated with Meshy.
