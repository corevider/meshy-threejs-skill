---
name: threejs-3d-generator-meshy
description: "Generate, texture, retexture, remesh, rig, animate, and download 3D assets for Three.js games using the Meshy API. Use for text-to-3D, image-to-3D, multi-view image-to-3D, game-ready GLB/FBX assets, characters, creatures, props, weapons, buildings, humanoid auto-rigging, a 678-clip mocap animation library, text-to-motion custom clips, retexturing, retopology/decimation, and browser asset pipelines. Ships a three.js clip viewer for verifying rigs and animations in a real browser. Use instead of threejs-3d-generator (Tripo) when the user asks for Meshy, when a humanoid character needs many animation clips, or when Tripo credits are exhausted. Pair with threejs-image-generator for concepts and reference images before image-to-3D."
---

# Three.js 3D Generator (Meshy)

## Purpose

Same job as `threejs-3d-generator`, different provider: Meshy instead of Tripo. Text-to-3D,
image-to-3D, multi-image-to-3D, retexture, remesh, humanoid auto-rig, animation from a
678-clip library or a text prompt, and downloadable GLB/FBX for browser games.

Pick the provider deliberately:

- **Meshy** for humanoid characters that need real animation coverage (678 mocap clips +
  text-to-motion vs Tripo's 16 presets), for explicit `pose_mode` control, for retexturing
  with multi-view references, and for one-call remesh/convert.
- **Tripo** (`threejs-3d-generator`) for NON-humanoid rigs - quadruped, avian, serpentine,
  aquatic - which Meshy's rigger rejects outright, and for stylization (lego/voxel/
  minecraft/voronoi) and part generation.

## API Key

Never store API keys in skill files or client-side game code. The script checks:

1. `--api-key`
2. `MESHY_API_KEY`

If the export lives in an interactive-only profile (a `~/.bashrc` line below the
non-interactive guard, which is Ubuntu's default), a plain `bash -c` will NOT see it. Probe
before declaring the key missing, and wrap invocations the same way:

```bash
bash -ic 'echo "MESHY_API_KEY=${MESHY_API_KEY:+SET}"'
bash -ic 'python3 ~/.claude/skills/threejs-3d-generator-meshy/scripts/meshy_3d_asset.py balance'
```

Paste the literal `MESHY_API_KEY=SET|MISSING` output in the report. Use the API only from
local/server-side tooling. Download URLs are retained ~3 days, so download immediately.

## Tool Script

Reference gate:

- Load `references/api-notes.md` before provider API work: endpoint/task choices, model
  versions, parameters, polling, rigging limits, animation library, retention, rate limits.
- Load `references/threejs-integration.md` before importing Meshy output into a browser game
  (rigged GLB + one-clip GLBs, root motion, scale/pivot, budget checks).
- `references/animations.csv` is the bundled animation library; search it through the script
  (`list-animations`) rather than reading the whole file into context.

Track required references in a reference ledger with yes/no, path, and failure reason. Do
not mark an asset pipeline complete while a required reference is skipped.

```bash
python3 ~/.claude/skills/threejs-3d-generator-meshy/scripts/meshy_3d_asset.py --help
```

## Common Commands

Check credits before a long run:

```bash
python3 .../meshy_3d_asset.py balance
```

Text to 3D (submits preview geometry, then auto-chains the texture/refine pass):

```bash
python3 .../meshy_3d_asset.py text \
  --prompt "game-ready sci-fi hover bike, sleek armored panels, readable silhouette, PBR" \
  --model-type standard --topology triangle --target-polycount 20000 \
  --texture-resolution 2k --pbr \
  --wait --download --out-dir assets/models/hover-bike --name hover-bike
```

Untextured geometry only (cheaper; texture it in-engine or later):

```bash
python3 .../meshy_3d_asset.py text --prompt "low poly stone crate prop" \
  --model-type lowpoly --target-polycount 3000 --no-refine --wait --download
```

Image to 3D from a local `threejs-image-generator` concept (local files become data URIs
automatically - there is no upload endpoint):

```bash
python3 .../meshy_3d_asset.py image --image assets/concepts/hover-bike-front.png \
  --texture-resolution 2k --pbr --wait --download --out-dir assets/models/hover-bike
```

Multi-view (1-4 images of the SAME subject; sharply better silhouettes than one view):

```bash
python3 .../meshy_3d_asset.py multi-image \
  --images concepts/front.png,concepts/side.png,concepts/back.png \
  --wait --download --out-dir assets/models/hero
```

Retexture, remesh, status, download:

```bash
python3 .../meshy_3d_asset.py retexture --input-task-id TASK_ID \
  --text-style-prompt "brushed gunmetal, orange hazard decals, worn edges" \
  --pbr --wait --download

python3 .../meshy_3d_asset.py remesh --input-task-id TASK_ID \
  --topology quad --target-polycount 8000 --wait --download

python3 .../meshy_3d_asset.py status TASK_ID
python3 .../meshy_3d_asset.py download TASK_ID --out-dir assets/models
```

Animation library search (678 clips; never guess an `action_id`):

```bash
python3 .../meshy_3d_asset.py list-animations --search sword
python3 .../meshy_3d_asset.py list-animations --category Fighting --limit 60
```

Rig and animate an existing humanoid:

```bash
python3 .../meshy_3d_asset.py rig --input-task-id TASK_ID --height-meters 1.8 \
  --wait --download --out-dir assets/models/hero

python3 .../meshy_3d_asset.py animate --rig-task-id RIG_TASK_ID \
  --animations idle,Walking_Woman,Running,4 \
  --wait --download --out-dir assets/models/hero --name hero
```

Custom motion from a prompt, then applied to the rig:

```bash
python3 .../meshy_3d_asset.py motion --prompt "spin a spear overhead then thrust forward" \
  --duration 3 --mode prime --wait
python3 .../meshy_3d_asset.py animate --rig-task-id RIG_TASK_ID \
  --motion-task-id MOTION_TASK_ID --wait --download
```

Whole character in one call (generate -> texture -> rig -> clips -> download, with rig and
clip QA in between):

```bash
python3 .../meshy_3d_asset.py character-pipeline \
  --prompt "stylized cyber runner character, game-ready outfit, readable silhouette" \
  --pose-mode t-pose --animations idle,walking,running \
  --out-dir assets/models/cyber-runner
```

## Rigging and Animation Reliability

Load `references/api-notes.md` for the parameter tables. The rules that prevent failures:

- **Humanoid only.** Meshy's rigger rejects quadrupeds, birds, dragons, vehicles. A creature
  that needs a skeleton goes to the Tripo skill; do not retry it here.
- Rig input must be TEXTURED, under 300,000 faces, and facing `+Z`. Generate with
  `--pose-mode t-pose` (or `a-pose`) - it is a real API parameter here, not prompt wishing.
- Generate characters as one fused mesh; keep props out of the silhouette.
- **Always pass `--target-polycount`** for anything that will be rigged or shipped to a
  browser. Without it the mesh comes back in the millions of faces (measured: 1.9M / 74 MB)
  and rigging rejects it at 320,000. `character-pipeline` defaults to 30,000 and auto-remeshes
  if the gate still trips.
- `height_meters` (default 1.7) sets the output scale, so the rigged GLB arrives in metres.
- The skeleton is Mixamo-like (`mixamorig:*`), so Mixamo clips and Meshy clips interoperate
  without bone renaming.
- One clip per animation task; the script batches a comma list into several tasks. Names in
  `--animations` are resolved against the bundled library (case-insensitive, prefix match) -
  when a token is ambiguous the script prints the alternatives it did not pick.
- After download, run `validate-rig rig.glb` (core + symmetric limb chains) and
  `validate-animation clip.glb` (scale tracks, limb-stretch translation tracks, per-clip
  duration/channel coverage), then verify motion visually in the engine.
- Clips are baked with root motion. Strip only the HORIZONTAL root translation at import;
  vertical carries jumps and gait bob. Snippet in `references/threejs-integration.md`.

## Verify in the Browser (rigged characters)

A rig and its clips are not verified until they have played in three.js. `validate-rig` and
`validate-animation` read the files; the viewer plays them on the actual skeleton and
measures the result, which is how the clip-rescale bug below was caught.

```bash
# 1. shrink for a fast page: clips lose their duplicated mesh, the rig its oversized texture
python3 ~/.claude/skills/threejs-3d-generator-meshy/scripts/slim_glb.py clip \
  assets/models/hero/hero-000-Idle-animation_glb.glb preview/idle.glb
python3 ~/.claude/skills/threejs-3d-generator-meshy/scripts/slim_glb.py rig \
  assets/models/hero/hero-rig-rigged_character_glb.glb preview/rig.glb --max-px 1024

# 2. serve the viewer next to the models (module imports need http, not file://)
cp ~/.claude/skills/threejs-3d-generator-meshy/viewer/clip-viewer.html preview/
cd preview && python3 -m http.server 8322

# 3. open, or drive it headless
#    http://localhost:8322/clip-viewer.html?rig=rig.glb&clips=idle.glb,walk.glb,run.glb
```

The viewer lists the clips, plays them on the rig, and shows per-clip measurements: track
counts before/after the import fixes, unresolved bone targets, hips travel, foot travel and
head height. The two import fixes are toggles, so turning one off shows the raw failure the
fix prevents. For automation it exposes `window.__ready`, `window.__report` (the same
measurements as JSON) and `window.__pose(clipIndex, seconds)` for deterministic frames; add
`&headless=1` to start paused.

What to check: `unresolved` is 0 on every clip (bone names match the rig), head height is
consistent across clips (see the rescale trap in `references/threejs-integration.md`), foot
travel is large and hips travel small once root motion is stripped, and the character reads
correctly in motion.

## Quality Rules

- Improve the user's prompt with material, silhouette, camera/readability, scale, and
  game-use constraints.
- Match `target_polycount` / `model_type` / `texture_resolution` to the browser budget at
  generation time; remeshing afterwards costs another task.
- `--topology quad` for anything deformed or edited later, `triangle` for static props.
- Keep `remove_lighting` on (default) so scene lighting does the work.
- Download immediately - assets expire in ~3 days.
- For a rigged character, play the clips in the viewer before calling the asset done, and
  report head height per clip along with the task IDs.
- Report the key probe output, reference ledger, task IDs (preview, refine, rig, each clip),
  output paths, model/type/topology/polycount/texture settings, credits consumed, remaining
  balance, Three.js import notes, and any missing/failed steps.
