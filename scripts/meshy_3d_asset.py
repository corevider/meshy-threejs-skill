#!/usr/bin/env python3
"""Small Meshy OpenAPI client for skill-driven 3D asset generation."""

from __future__ import annotations

import argparse
import base64
import csv
import json
import math
import mimetypes
import os
from pathlib import Path
import struct
import sys
import time
from typing import Any
from urllib import error, parse, request

BASE_URL = "https://api.meshy.ai"

ENDPOINTS = {
    "text": "/openapi/v2/text-to-3d",
    "image": "/openapi/v1/image-to-3d",
    "multi-image": "/openapi/v1/multi-image-to-3d",
    "retexture": "/openapi/v1/retexture",
    "remesh": "/openapi/v1/remesh",
    "rig": "/openapi/v1/rigging",
    "animate": "/openapi/v1/animations",
    "motion": "/openapi/v1/text-to-motion",
}
KIND_ORDER = ("text", "image", "multi-image", "rig", "animate", "retexture", "remesh", "motion")

FINAL_STATUSES = {"SUCCEEDED", "FAILED", "CANCELED"}
FORMATS = ("glb", "fbx", "obj", "stl", "usdz", "3mf", "blend")
DATA_URI_WARN_BYTES = 9 * 1024 * 1024

ACTION_ALIASES = {
    "walk": "walking",
    "run": "running",
    "jump": "jumping",
    "climb": "climbing",
    "dance": "dancing",
    "punch": "punching",
    "block": "blocking",
    "die": "dying",
    "death": "dying",
    "hit": "gettinghit",
    "cast": "castingspell",
    "attack": "attackingwithweapon",
    "swim": "swimming",
    "fall": "fallingfreely",
    "turn": "turningaround",
    "crouch": "crouchwalking",
    "pickup": "pickingupitem",
    "sleep": "sleeping",
}

RIG_FACE_LIMIT = 300_000
RIG_FACE_BUDGET = 60_000

ANIMATIONS_CSV = Path(__file__).resolve().parent.parent / "references" / "animations.csv"

HUMANOID_CORE = ("Hips", "Spine", "Head")
HUMANOID_PAIRED = ("Arm", "ForeArm", "Hand", "UpLeg", "Leg", "Foot")


class MeshyError(RuntimeError):
    pass


def eprint(*parts: object) -> None:
    print(*parts, file=sys.stderr)


def api_key_from(args: argparse.Namespace) -> str:
    key = getattr(args, "api_key", None) or os.environ.get("MESHY_API_KEY")
    if not key:
        raise MeshyError(
            "Missing API key. Set MESHY_API_KEY or pass --api-key. If the export lives in an "
            "interactive-only profile (~/.bashrc below its non-interactive guard), run through: "
            "bash -ic 'python3 ...'"
        )
    return key


def api_request(api_key: str, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    url = f"{BASE_URL}{path}"
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {api_key}")
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8")
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise MeshyError(f"HTTP {exc.code} {exc.reason}: {raw}") from exc
    except error.URLError as exc:
        raise MeshyError(f"Request failed: {exc.reason}") from exc
    if not raw:
        return {}
    return json.loads(raw)


def endpoint_for(kind: str) -> str:
    if kind not in ENDPOINTS:
        raise MeshyError(f"Unknown task kind {kind!r}. Valid: {', '.join(ENDPOINTS)}")
    return ENDPOINTS[kind]


def create_task(api_key: str, kind: str, payload: dict[str, Any]) -> str:
    data = api_request(api_key, "POST", endpoint_for(kind), payload)
    task_id = data.get("result") if isinstance(data, dict) else None
    if isinstance(task_id, dict):
        task_id = task_id.get("id")
    if not isinstance(task_id, str) or not task_id:
        raise MeshyError(f"Unexpected create response: {json.dumps(data)[:400]}")
    return task_id


def get_task(api_key: str, kind: str, task_id: str) -> dict[str, Any]:
    data = api_request(api_key, "GET", f"{endpoint_for(kind)}/{parse.quote(task_id)}")
    # Rig/animate tasks carry their assets in a nested "result" object, so only unwrap an
    # envelope that is not itself a task.
    if isinstance(data, dict) and "status" not in data and isinstance(data.get("result"), dict):
        data = data["result"]
    if not isinstance(data, dict):
        raise MeshyError(f"Unexpected task response: {json.dumps(data)[:400]}")
    return data


def discover_kind(api_key: str, task_id: str, hint: str | None = None) -> tuple[str, dict[str, Any]]:
    if hint and hint not in ENDPOINTS:
        raise MeshyError(f"Unknown --kind {hint!r}. Valid: {', '.join(ENDPOINTS)}")
    order = [hint] if hint else list(KIND_ORDER)
    for kind in order:
        try:
            return kind, get_task(api_key, kind, task_id)
        except MeshyError as exc:
            if "HTTP 404" in str(exc) or "HTTP 400" in str(exc):
                continue
            raise
    raise MeshyError(f"Task {task_id} not found on any endpoint. Pass --kind to disambiguate.")


def wait_for_task(api_key: str, kind: str, task_id: str, interval: int, timeout: int) -> dict[str, Any]:
    deadline = time.time() + timeout
    last = ""
    while True:
        task = get_task(api_key, kind, task_id)
        status = str(task.get("status", "UNKNOWN"))
        line = f"{status} {task.get('progress', 0)}%"
        if line != last:
            eprint(f"[{kind}:{task_id}] {line}")
            last = line
        if status in FINAL_STATUSES:
            if status != "SUCCEEDED":
                err = task.get("task_error") or {}
                raise MeshyError(f"Task {task_id} ended {status}: {err.get('message', '')}".strip())
            return task
        if time.time() > deadline:
            raise MeshyError(f"Timed out after {timeout}s waiting for {task_id} (last: {line})")
        time.sleep(interval)


def safe_name(value: str) -> str:
    out = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in value).strip("-")
    return (out or "meshy")[:60]


def collect_urls(node: Any, prefix: str = "") -> list[tuple[str, str]]:
    """Walk a task object and return (label, url) for every downloadable asset."""
    found: list[tuple[str, str]] = []
    if isinstance(node, str):
        if node.startswith("http") and prefix:
            found.append((prefix, node))
        return found
    if isinstance(node, dict):
        for key, value in node.items():
            label = key[:-4] if key.endswith("_url") else key
            found.extend(collect_urls(value, f"{prefix}-{label}" if prefix else label))
        return found
    if isinstance(node, list):
        for idx, value in enumerate(node):
            found.extend(collect_urls(value, f"{prefix}{idx}" if prefix else str(idx)))
    return found


def extension_for(url: str, label: str, content_type: str | None) -> str:
    path = parse.urlparse(url).path
    ext = Path(path).suffix
    if ext and len(ext) <= 6:
        return ext
    for fmt in FORMATS + ("png", "jpg", "gif", "bvh", "mtl"):
        if label.endswith(fmt) or f"-{fmt}" in label:
            return f".{fmt}"
    if content_type:
        guess = mimetypes.guess_extension(content_type.split(";")[0].strip())
        if guess:
            return guess
    return ".bin"


def download_one(url: str, out_dir: Path, base: str, label: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    with request.urlopen(url, timeout=300) as resp:
        content_type = resp.headers.get("Content-Type")
        payload = resp.read()
    target = out_dir / f"{base}-{safe_name(label)}{extension_for(url, label, content_type)}"
    target.write_bytes(payload)
    return target


def clean_label(label: str) -> str:
    for noise in ("model_urls-", "result-", "urls-"):
        label = label.replace(noise, "")
    return label


def download_outputs(task: dict[str, Any], out_dir: Path, base: str | None = None,
                     formats: set[str] | None = None) -> list[Path]:
    """formats filters model files only (Meshy returns every format it baked);
    thumbnails, textures, rigs and clips always come through."""
    base = base or safe_name(str(task.get("id", "task"))[:12])
    written: list[Path] = []
    for raw_label, url in collect_urls(task):
        if raw_label.startswith(("task_error", "preceding")):
            continue
        label = clean_label(raw_label)
        suffix = label.rsplit("-", 1)[-1].rsplit("_", 1)[-1]
        if formats and suffix in FORMATS and suffix not in formats:
            continue
        try:
            path = download_one(url, out_dir, base, label)
        except Exception as exc:  # noqa: BLE001 - one bad URL must not kill the batch
            eprint(f"  ! failed {label}: {exc}")
            continue
        written.append(path)
        print(f"  saved {path}")
    if not written:
        eprint("  (no downloadable URLs in task result)")
    return written


def image_ref(value: str) -> str:
    """Meshy takes a public URL or a base64 data URI; local files become data URIs."""
    if value.startswith(("http://", "https://", "data:")):
        return value
    path = Path(value).expanduser()
    if not path.is_file():
        raise MeshyError(f"File not found: {path}")
    size = path.stat().st_size
    if size > DATA_URI_WARN_BYTES:
        eprint(f"! {path.name} is {size / 1e6:.1f} MB; large data URIs may be rejected.")
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def csv_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def load_animation_library() -> list[dict[str, str]]:
    if not ANIMATIONS_CSV.is_file():
        raise MeshyError(f"Animation library missing: {ANIMATIONS_CSV}")
    with ANIMATIONS_CSV.open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def resolve_action(token: str) -> tuple[int, str]:
    token = token.strip()
    library = load_animation_library()
    if token.isdigit():
        wanted = int(token)
        for row in library:
            if int(row["action_id"]) == wanted:
                return wanted, row["name"]
        return wanted, "id-not-in-bundled-library"
    lower = ACTION_ALIASES.get(token.lower(), token.lower())
    exact = [r for r in library if r["name"].lower() == lower]
    if exact:
        return int(exact[0]["action_id"]), exact[0]["name"]
    # A bare gameplay word ("walk", "run", "jump") means the plain locomotion clip, not the
    # first alphabetical name containing it - match the subcategory and take the canonical
    # lowest id (Idle=0, Walking_Woman=1, Run_02=14) instead of Running_Reload or walking_2.
    subcat = [r for r in library if r["subcategory"].lower() == lower]
    if subcat:
        best = min(subcat, key=lambda r: int(r["action_id"]))
        return int(best["action_id"]), best["name"]
    starts = [r for r in library if r["name"].lower().startswith(lower)]
    pool = starts or [r for r in library if lower in r["name"].lower()]
    if not pool:
        raise MeshyError(
            f"No animation matches {token!r}. Browse with: meshy_3d_asset.py list-animations --search {token}"
        )
    pool.sort(key=lambda r: (len(r["name"]), int(r["action_id"])))
    best = pool[0]
    if len(pool) > 1:
        others = ", ".join(f"{r['name']}({r['action_id']})" for r in pool[1:6])
        eprint(f"  {token!r} -> {best['name']} (id {best['action_id']}); other matches: {others}")
    return int(best["action_id"]), best["name"]


def apply_optional(payload: dict[str, Any], args: argparse.Namespace, mapping: dict[str, str]) -> None:
    for attr, field in mapping.items():
        value = getattr(args, attr, None)
        if value is None or value is False:
            continue
        payload[field] = value


def wanted_formats(args: argparse.Namespace) -> set[str] | None:
    if getattr(args, "all_formats", False):
        return None
    return set(csv_list(getattr(args, "download_formats", None) or "glb"))


def maybe_finish(api_key: str, kind: str, task_id: str, args: argparse.Namespace,
                 base: str | None = None) -> dict[str, Any] | None:
    print(f"{kind} task: {task_id}")
    if not getattr(args, "wait", False):
        print(f"Poll with: status {task_id} --kind {kind}")
        return None
    task = wait_for_task(api_key, kind, task_id, args.interval, args.timeout)
    print(f"credits: {task.get('consumed_credits')}")
    if getattr(args, "download", False):
        download_outputs(task, Path(args.out_dir), base, wanted_formats(args))
    return task


def apply_remesh_flag(payload: dict[str, Any], args: argparse.Namespace) -> None:
    """target_polycount and topology are IGNORED unless should_remesh is on, and its
    default varies by model: a 20k request came back with 1.9M faces (too dense to rig)."""
    if getattr(args, "no_remesh", False):
        payload["should_remesh"] = False
    elif payload.get("target_polycount") or payload.get("topology"):
        payload["should_remesh"] = True


def build_text_payload(args: argparse.Namespace) -> dict[str, Any]:
    payload: dict[str, Any] = {"mode": "preview", "prompt": args.prompt}
    apply_optional(payload, args, {
        "ai_model": "ai_model",
        "model_type": "model_type",
        "art_style": "art_style",
        "pose_mode": "pose_mode",
        "topology": "topology",
        "target_polycount": "target_polycount",
        "decimation_mode": "decimation_mode",
        "negative_prompt": "negative_prompt",
        "seed": "seed",
        "ultra": "ultra_mode",
        "auto_size": "auto_size",
        "origin_at": "origin_at",
        "alpha_thumbnail": "alpha_thumbnail",
    })
    apply_remesh_flag(payload, args)
    formats = csv_list(getattr(args, "target_formats", None))
    if formats:
        payload["target_formats"] = formats
    return payload


def build_refine_payload(args: argparse.Namespace, preview_task_id: str) -> dict[str, Any]:
    payload: dict[str, Any] = {"mode": "refine", "preview_task_id": preview_task_id}
    apply_optional(payload, args, {
        "ai_model": "ai_model",
        "texture_resolution": "texture_resolution",
        "texture_prompt": "texture_prompt",
        "pbr": "enable_pbr",
        "auto_size": "auto_size",
        "origin_at": "origin_at",
        "alpha_thumbnail": "alpha_thumbnail",
    })
    if getattr(args, "texture_image", None):
        payload["texture_image_url"] = image_ref(args.texture_image)
    if getattr(args, "keep_lighting", False):
        payload["remove_lighting"] = False
    formats = csv_list(getattr(args, "target_formats", None))
    if formats:
        payload["target_formats"] = formats
    return payload


def cmd_text(args: argparse.Namespace) -> None:
    api_key = api_key_from(args)
    preview_id = create_task(api_key, "text", build_text_payload(args))
    base = safe_name(args.name or args.prompt[:40])
    print(f"preview task: {preview_id}")
    if not args.wait:
        print(f"Poll with: status {preview_id} --kind text")
        print(f"Then texture with: refine --preview-task-id {preview_id} --wait --download")
        return
    preview = wait_for_task(api_key, "text", preview_id, args.interval, args.timeout)
    if args.no_refine:
        if args.download:
            download_outputs(preview, Path(args.out_dir), f"{base}-preview", wanted_formats(args))
        return
    refine_id = create_task(api_key, "text", build_refine_payload(args, preview_id))
    print(f"refine task: {refine_id}")
    task = wait_for_task(api_key, "text", refine_id, args.interval, args.timeout)
    print(f"credits: {task.get('consumed_credits')}")
    if args.download:
        download_outputs(task, Path(args.out_dir), base, wanted_formats(args))


def cmd_refine(args: argparse.Namespace) -> None:
    api_key = api_key_from(args)
    task_id = create_task(api_key, "text", build_refine_payload(args, args.preview_task_id))
    maybe_finish(api_key, "text", task_id, args, safe_name(args.name or "refine"))


def build_image_payload(args: argparse.Namespace) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    apply_optional(payload, args, {
        "ai_model": "ai_model",
        "model_type": "model_type",
        "topology": "topology",
        "target_polycount": "target_polycount",
        "decimation_mode": "decimation_mode",
        "texture_resolution": "texture_resolution",
        "texture_prompt": "texture_prompt",
        "pbr": "enable_pbr",
        "pose_mode": "pose_mode",
        "ultra": "ultra_mode",
        "auto_size": "auto_size",
        "origin_at": "origin_at",
        "alpha_thumbnail": "alpha_thumbnail",
        "multi_view_thumbnails": "multi_view_thumbnails",
    })
    if args.no_texture:
        payload["should_texture"] = False
    apply_remesh_flag(payload, args)
    if args.no_image_enhancement:
        payload["image_enhancement"] = False
    if args.keep_lighting:
        payload["remove_lighting"] = False
    if getattr(args, "texture_image", None):
        payload["texture_image_url"] = image_ref(args.texture_image)
    formats = csv_list(args.target_formats)
    if formats:
        payload["target_formats"] = formats
    return payload


def cmd_image(args: argparse.Namespace) -> None:
    api_key = api_key_from(args)
    payload = build_image_payload(args)
    payload["image_url"] = image_ref(args.image)
    task_id = create_task(api_key, "image", payload)
    maybe_finish(api_key, "image", task_id, args, safe_name(args.name or Path(args.image).stem))


def cmd_multi_image(args: argparse.Namespace) -> None:
    api_key = api_key_from(args)
    images = csv_list(args.images)
    if not 1 <= len(images) <= 4:
        raise MeshyError("--images takes 1 to 4 comma-separated paths or URLs.")
    payload = build_image_payload(args)
    payload["image_urls"] = [image_ref(item) for item in images]
    task_id = create_task(api_key, "multi-image", payload)
    maybe_finish(api_key, "multi-image", task_id, args, safe_name(args.name or "multiview"))


def source_payload(args: argparse.Namespace) -> dict[str, Any]:
    if bool(args.input_task_id) == bool(args.model_url):
        raise MeshyError("Pass exactly one of --input-task-id or --model-url.")
    if args.input_task_id:
        return {"input_task_id": args.input_task_id}
    return {"model_url": image_ref(args.model_url)}


def cmd_retexture(args: argparse.Namespace) -> None:
    api_key = api_key_from(args)
    payload = source_payload(args)
    styles = [bool(args.text_style_prompt), bool(args.image_style), bool(args.multiview_images)]
    if sum(styles) != 1:
        raise MeshyError("Pass exactly one of --text-style-prompt, --image-style, --multiview-images.")
    if args.text_style_prompt:
        payload["text_style_prompt"] = args.text_style_prompt
    if args.image_style:
        payload["image_style_url"] = image_ref(args.image_style)
    if args.multiview_images:
        payload["multiview_image_urls"] = [image_ref(i) for i in csv_list(args.multiview_images)]
        payload.setdefault("ai_model", "meshy-7")
    apply_optional(payload, args, {
        "ai_model": "ai_model",
        "texture_resolution": "texture_resolution",
        "pbr": "enable_pbr",
        "original_uv": "enable_original_uv",
        "alpha_thumbnail": "alpha_thumbnail",
    })
    if args.keep_lighting:
        payload["remove_lighting"] = False
    formats = csv_list(args.target_formats)
    if formats:
        payload["target_formats"] = formats
    task_id = create_task(api_key, "retexture", payload)
    maybe_finish(api_key, "retexture", task_id, args, safe_name(args.name or "retexture"))


def cmd_remesh(args: argparse.Namespace) -> None:
    api_key = api_key_from(args)
    payload = source_payload(args)
    apply_optional(payload, args, {
        "topology": "topology",
        "target_polycount": "target_polycount",
        "decimation_mode": "decimation_mode",
    })
    payload["target_formats"] = csv_list(args.target_formats) or ["glb"]
    task_id = create_task(api_key, "remesh", payload)
    maybe_finish(api_key, "remesh", task_id, args, safe_name(args.name or "remesh"))


def cmd_rig(args: argparse.Namespace) -> None:
    api_key = api_key_from(args)
    payload = source_payload(args)
    if args.height_meters is not None:
        payload["height_meters"] = args.height_meters
    if args.texture_image:
        payload["texture_image_url"] = image_ref(args.texture_image)
    task_id = create_task(api_key, "rig", payload)
    maybe_finish(api_key, "rig", task_id, args, safe_name(args.name or "rig"))


def submit_animation(api_key: str, rig_task_id: str, action: str | None, motion_task_id: str | None,
                     fps: int | None) -> tuple[str, str]:
    payload: dict[str, Any] = {"rig_task_id": rig_task_id}
    if motion_task_id:
        payload["motion_task_id"] = motion_task_id
        label = f"motion-{motion_task_id[:8]}"
    else:
        action_id, name = resolve_action(action or "")
        payload["action_id"] = action_id
        label = f"{action_id:03d}-{safe_name(name)}"
    if fps:
        payload["post_process"] = {"operation_type": "change_fps", "fps": fps}
    return create_task(api_key, "animate", payload), label


def cmd_animate(args: argparse.Namespace) -> None:
    api_key = api_key_from(args)
    if bool(args.animations) == bool(args.motion_task_id):
        raise MeshyError("Pass exactly one of --animations or --motion-task-id.")
    jobs: list[tuple[str, str]] = []
    if args.motion_task_id:
        jobs.append(submit_animation(api_key, args.rig_task_id, None, args.motion_task_id, args.fps))
    else:
        for token in csv_list(args.animations):
            jobs.append(submit_animation(api_key, args.rig_task_id, token, None, args.fps))
    for task_id, label in jobs:
        print(f"animate task {task_id} ({label})")
    if not args.wait:
        return
    for task_id, label in jobs:
        task = wait_for_task(api_key, "animate", task_id, args.interval, args.timeout)
        if args.download:
            download_outputs(task, Path(args.out_dir), f"{safe_name(args.name or 'clip')}-{label}",
                             wanted_formats(args))


def cmd_motion(args: argparse.Namespace) -> None:
    api_key = api_key_from(args)
    payload = {"prompt": args.prompt, "duration": args.duration, "mode": args.mode}
    task_id = create_task(api_key, "motion", payload)
    maybe_finish(api_key, "motion", task_id, args, safe_name(args.name or args.prompt[:30]))


def cmd_status(args: argparse.Namespace) -> None:
    api_key = api_key_from(args)
    kind, task = discover_kind(api_key, args.task_id, args.kind)
    print(f"kind: {kind}")
    print(json.dumps(task, indent=2)[:6000])


def cmd_download(args: argparse.Namespace) -> None:
    api_key = api_key_from(args)
    kind, task = discover_kind(api_key, args.task_id, args.kind)
    if task.get("status") != "SUCCEEDED":
        raise MeshyError(f"Task {args.task_id} is {task.get('status')}, nothing to download.")
    download_outputs(task, Path(args.out_dir), safe_name(args.name) if args.name else None,
                     wanted_formats(args))


def cmd_balance(args: argparse.Namespace) -> None:
    api_key = api_key_from(args)
    print(json.dumps(api_request(api_key, "GET", "/openapi/v1/balance"), indent=2))


def cmd_list_animations(args: argparse.Namespace) -> None:
    library = load_animation_library()
    needle = (args.search or "").lower()
    category = (args.category or "").lower()
    rows = [
        r for r in library
        if (not needle or needle in r["name"].lower() or needle in r["subcategory"].lower())
        and (not category or category in r["category"].lower())
    ]
    for row in rows[:args.limit]:
        print(f"{row['action_id']:>4}  {row['name']:<38} {row['category']}/{row['subcategory']}")
    print(f"-- {len(rows)} match(es), showing {min(len(rows), args.limit)} of {len(library)} animations")


def cmd_character_pipeline(args: argparse.Namespace) -> None:
    api_key = api_key_from(args)
    out_dir = Path(args.out_dir)
    base = safe_name(args.name or (args.prompt or "character")[:40])

    model_task_id = args.model_task_id
    if not model_task_id:
        if not args.prompt:
            raise MeshyError("Pass --prompt or --model-task-id.")
        pose_words = "full body, arms away from the body, symmetric, facing forward, no props fused to the body"
        prompt = args.prompt if "full body" in args.prompt.lower() else f"{args.prompt}, {pose_words}"
        preview_args = argparse.Namespace(**vars(args))
        preview_args.prompt = prompt
        preview_id = create_task(api_key, "text", build_text_payload(preview_args))
        print(f"preview task: {preview_id}")
        wait_for_task(api_key, "text", preview_id, args.interval, args.timeout)
        refine_id = create_task(api_key, "text", build_refine_payload(args, preview_id))
        print(f"refine task: {refine_id}")
        task = wait_for_task(api_key, "text", refine_id, args.interval, args.timeout)
        download_outputs(task, out_dir, f"{base}-model", wanted_formats(args))
        model_task_id = refine_id

    def submit_rig(source_task_id: str) -> str:
        rig_payload: dict[str, Any] = {"input_task_id": source_task_id}
        if args.height_meters is not None:
            rig_payload["height_meters"] = args.height_meters
        return create_task(api_key, "rig", rig_payload)

    try:
        rig_id = submit_rig(model_task_id)
    except MeshyError as exc:
        if "face limit" not in str(exc) and "exceeds" not in str(exc):
            raise
        eprint("rig rejected the mesh density; remeshing first")
        budget = min(args.target_polycount or RIG_FACE_BUDGET, RIG_FACE_LIMIT)
        remesh_id = create_task(api_key, "remesh", {
            "input_task_id": model_task_id,
            "target_polycount": budget,
            "topology": args.topology or "triangle",
            "target_formats": ["glb"],
        })
        print(f"remesh task: {remesh_id} (target_polycount {budget})")
        remesh_task = wait_for_task(api_key, "remesh", remesh_id, args.interval, args.timeout)
        download_outputs(remesh_task, out_dir, f"{base}-remesh", wanted_formats(args))
        model_task_id = remesh_id
        rig_id = submit_rig(remesh_id)
    print(f"rig task: {rig_id}")
    rig_task = wait_for_task(api_key, "rig", rig_id, args.interval, args.timeout)
    rig_files = download_outputs(rig_task, out_dir, f"{base}-rig", wanted_formats(args))

    for path in rig_files:
        if path.suffix == ".glb" and "character" in path.name:
            description, problems = validate_rig_glb(path)
            print(f"rig check: {description}")
            for problem in problems:
                eprint(f"  ! {problem}")
            break

    for token in csv_list(args.animations):
        task_id, label = submit_animation(api_key, rig_id, token, None, args.fps)
        print(f"animate task {task_id} ({label})")
        task = wait_for_task(api_key, "animate", task_id, args.interval, args.timeout)
        for clip in download_outputs(task, out_dir, f"{base}-{label}", wanted_formats(args)):
            if clip.suffix == ".glb":
                report, problems = validate_animation_glb(clip)
                for line in report:
                    print(f"  {line}")
                for problem in problems:
                    eprint(f"  ! {problem}")

    print(f"\nmodel task: {model_task_id}\nrig task: {rig_id}\noutput: {out_dir}")


def load_glb(path: Path) -> tuple[dict[str, Any], bytes]:
    data = path.read_bytes()
    if data[:4] != b"glTF":
        raise MeshyError(f"Not a GLB file: {path}")
    offset = 12
    gltf: dict[str, Any] | None = None
    bin_chunk = b""
    while offset < len(data):
        clen, ctype = struct.unpack_from("<II", data, offset)
        chunk = data[offset + 8:offset + 8 + clen]
        if ctype == 0x4E4F534A:
            gltf = json.loads(chunk)
        elif ctype == 0x004E4942:
            bin_chunk = chunk
        offset += 8 + clen
    if gltf is None:
        raise MeshyError(f"No JSON chunk in GLB: {path}")
    return gltf, bin_chunk


def _read_accessor(gltf: dict[str, Any], bin_chunk: bytes, idx: int) -> list[tuple[float, ...]]:
    comp = {5126: ("f", 4), 5123: ("H", 2), 5125: ("I", 4)}
    ncomp = {"SCALAR": 1, "VEC3": 3, "VEC4": 4}
    acc = gltf["accessors"][idx]
    bv = gltf["bufferViews"][acc["bufferView"]]
    start = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
    n = ncomp[acc["type"]]
    fmt, _ = comp[acc["componentType"]]
    count = acc["count"]
    vals = struct.unpack_from(f"<{count * n}{fmt}", bin_chunk, start)
    return [vals[i * n:(i + 1) * n] for i in range(count)]


def validate_rig_glb(path: Path) -> tuple[str, list[str]]:
    """Meshy auto-rigs are humanoid with Mixamo-like bone names. Check the core chain
    exists and that left/right limbs are present and equally deep."""
    gltf, _ = load_glb(path)
    names = [n.get("name", "") for n in gltf.get("nodes", []) if n.get("name")]
    bones = [n.split(":")[-1] for n in names]
    joined = " ".join(bones).lower()
    problems: list[str] = []
    if not bones:
        return "no named nodes", ["rig GLB has no named nodes"]
    for core in HUMANOID_CORE:
        if core.lower() not in joined:
            problems.append(f"missing core bone {core}")
    for part in HUMANOID_PAIRED:
        left = sum(1 for b in bones if b.lower().startswith("left") and part.lower() in b.lower())
        right = sum(1 for b in bones if b.lower().startswith("right") and part.lower() in b.lower())
        if left == 0 or right == 0:
            problems.append(f"missing Left/Right {part} (L={left} R={right})")
        elif abs(left - right) > 1:
            problems.append(f"{part} chain asymmetric (L={left} R={right})")
    if len(bones) < 20:
        problems.append(f"suspiciously small skeleton ({len(bones)} nodes)")
    return f"{len(bones)} nodes, e.g. {', '.join(bones[:8])}", problems


def validate_animation_glb(path: Path) -> tuple[list[str], list[str]]:
    """Keyframe-level QA. Warp signatures: scale tracks, or translation tracks on
    non-root bones that deviate far from the bone's rest offset (limb stretching)."""
    gltf, bin_chunk = load_glb(path)
    nodes = gltf.get("nodes", [])
    roots = {"armature", "root", "hips", "hip", "pelvis"}
    report: list[str] = []
    problems: list[str] = []
    animations = gltf.get("animations", [])
    if not animations:
        return ["no animations in file"], ["no animation clips found"]
    for anim in animations:
        dur = 0.0
        flat_scale = 0
        rescale: dict[str, float] = {}
        rot_bones: set[str] = set()
        big_rot: dict[str, int] = {}
        for ch in anim["channels"]:
            sampler = anim["samplers"][ch["sampler"]]
            times = _read_accessor(gltf, bin_chunk, sampler["input"])
            out = _read_accessor(gltf, bin_chunk, sampler["output"])
            node = nodes[ch["target"]["node"]] if ch["target"].get("node") is not None else {}
            name = node.get("name", "?")
            dur = max(dur, times[-1][0])
            path_kind = ch["target"]["path"]
            if path_kind == "rotation":
                rot_bones.add(name)
                first = out[0]
                amp = 0.0
                for quat in out:
                    dot = abs(sum(a * b for a, b in zip(first, quat)))
                    amp = max(amp, 2 * math.acos(min(1.0, dot)))
                if math.degrees(amp) > 170:
                    big_rot[name] = round(math.degrees(amp))
            elif path_kind == "scale":
                # Meshy bakes a constant scale track on every bone; only a track that
                # actually leaves the rest scale can warp the mesh.
                rest = node.get("scale", [1.0, 1.0, 1.0])
                spread = max(max(v[i] for v in out) - min(v[i] for v in out) for i in range(3))
                offset = max(abs(out[0][i] - rest[i]) for i in range(3))
                if spread > 0.05:
                    problems.append(
                        f"{anim.get('name')}: scale track on {name} varies by {spread:.2f} (warp risk)"
                    )
                elif offset > 0.05:
                    # Constant but off rest: the clip was authored for a different rig size,
                    # so this clip renders the character larger/smaller than its neighbours.
                    rescale[name] = round(out[0][0], 3)
                else:
                    flat_scale += 1
            elif path_kind == "translation" and name.split(":")[-1].lower() not in roots:
                rest = node.get("translation", [0, 0, 0])
                restlen = math.sqrt(sum(c * c for c in rest)) or 1e-9
                dev = max(math.sqrt(sum((v[i] - rest[i]) ** 2 for i in range(3))) for v in out)
                if dev / restlen > 0.5:
                    problems.append(
                        f"{anim.get('name')}: translation track on non-root bone {name} deviates "
                        f"{dev / restlen:.1f}x its rest offset (limb stretch warp)"
                    )
        report.append(
            f"{anim.get('name')}: {dur:.2f}s, {len(anim['channels'])} channels, "
            f"{len(rot_bones)} bones rotating, {flat_scale} constant scale tracks"
        )
        if rescale:
            report.append(f"  constant rescale vs rest pose (size mismatch between clips): {rescale}")
        if big_rot:
            report.append(f"  rotation amplitude >170deg (check visually): {big_rot}")
    return report, problems


def cmd_validate_rig(args: argparse.Namespace) -> None:
    description, problems = validate_rig_glb(Path(args.glb_path))
    print(description)
    if problems:
        raise MeshyError("Rig validation failed: " + "; ".join(problems))
    print("Rig looks structurally valid.")


def cmd_validate_animation(args: argparse.Namespace) -> None:
    report, problems = validate_animation_glb(Path(args.glb_path))
    for line in report:
        print(line)
    if problems:
        raise MeshyError("Animation validation failed: " + "; ".join(problems))
    print("Clips look structurally sound (verify motion visually in the engine).")


def add_runtime_args(parser: argparse.ArgumentParser, default_out: str = "meshy-output") -> None:
    parser.add_argument("--api-key")
    parser.add_argument("--name", help="filename base for downloads")
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--out-dir", default=default_out)
    parser.add_argument("--download-formats", default="glb",
                        help="which model formats to save (default glb; Meshy bakes all of them)")
    parser.add_argument("--all-formats", action="store_true", help="save every returned model format")
    parser.add_argument("--interval", type=int, default=6)
    parser.add_argument("--timeout", type=int, default=900)


def add_geometry_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--ai-model", choices=["meshy-5", "meshy-6", "meshy-7", "latest"])
    parser.add_argument("--model-type", choices=["standard", "smart-topology", "lowpoly"])
    parser.add_argument("--topology", choices=["quad", "triangle"])
    parser.add_argument("--target-polycount", type=int)
    parser.add_argument("--decimation-mode", type=int, choices=[1, 2, 3, 4])
    parser.add_argument("--no-remesh", action="store_true")
    parser.add_argument("--ultra", action="store_true", help="ultra_mode (meshy-7 only)")
    parser.add_argument("--auto-size", action="store_true")
    parser.add_argument("--origin-at", choices=["bottom", "center"])
    parser.add_argument("--alpha-thumbnail", action="store_true")
    parser.add_argument("--target-formats", help="comma list: glb,fbx,obj,stl,usdz,3mf")


def add_texture_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--texture-resolution", choices=["2k", "4k", "8k"])
    parser.add_argument("--texture-prompt")
    parser.add_argument("--texture-image", help="style image path or URL")
    parser.add_argument("--pbr", action="store_true", help="enable_pbr")
    parser.add_argument("--keep-lighting", action="store_true", help="remove_lighting=false")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Meshy OpenAPI client for Three.js asset pipelines")
    sub = parser.add_subparsers(dest="command", required=True)

    text = sub.add_parser("text", help="text to 3D (preview, then refine/texture)")
    text.add_argument("--prompt", required=True)
    text.add_argument("--negative-prompt")
    text.add_argument("--art-style", choices=["realistic", "sculpture"])
    text.add_argument("--pose-mode", choices=["a-pose", "t-pose", "empty"])
    text.add_argument("--seed", type=int)
    text.add_argument("--no-refine", action="store_true", help="stop after the untextured preview")
    add_geometry_args(text)
    add_texture_args(text)
    add_runtime_args(text)
    text.set_defaults(func=cmd_text)

    refine = sub.add_parser("refine", help="texture an existing preview task")
    refine.add_argument("--preview-task-id", required=True)
    refine.add_argument("--ai-model", choices=["meshy-5", "meshy-6", "meshy-7", "latest"])
    refine.add_argument("--auto-size", action="store_true")
    refine.add_argument("--origin-at", choices=["bottom", "center"])
    refine.add_argument("--alpha-thumbnail", action="store_true")
    refine.add_argument("--target-formats")
    add_texture_args(refine)
    add_runtime_args(refine)
    refine.set_defaults(func=cmd_refine)

    image = sub.add_parser("image", help="image to 3D (local path or URL)")
    image.add_argument("--image", required=True)
    image.add_argument("--pose-mode", choices=["a-pose", "t-pose", "empty"])
    image.add_argument("--no-texture", action="store_true")
    image.add_argument("--no-image-enhancement", action="store_true")
    image.add_argument("--multi-view-thumbnails", action="store_true")
    add_geometry_args(image)
    add_texture_args(image)
    add_runtime_args(image)
    image.set_defaults(func=cmd_image)

    multi = sub.add_parser("multi-image", help="1-4 view images to 3D")
    multi.add_argument("--images", required=True, help="comma list of paths or URLs (1-4)")
    multi.add_argument("--pose-mode", choices=["a-pose", "t-pose", "empty"])
    multi.add_argument("--no-texture", action="store_true")
    multi.add_argument("--no-image-enhancement", action="store_true")
    multi.add_argument("--multi-view-thumbnails", action="store_true")
    add_geometry_args(multi)
    add_texture_args(multi)
    add_runtime_args(multi)
    multi.set_defaults(func=cmd_multi_image)

    retex = sub.add_parser("retexture", help="restyle an existing model's textures")
    retex.add_argument("--input-task-id")
    retex.add_argument("--model-url", help="public URL or local .glb/.obj/.fbx path")
    retex.add_argument("--text-style-prompt")
    retex.add_argument("--image-style")
    retex.add_argument("--multiview-images", help="1-4 images, requires meshy-7")
    retex.add_argument("--ai-model", choices=["meshy-5", "meshy-6", "meshy-7", "latest"])
    retex.add_argument("--original-uv", action="store_true", help="enable_original_uv")
    retex.add_argument("--alpha-thumbnail", action="store_true")
    retex.add_argument("--target-formats")
    add_texture_args(retex)
    add_runtime_args(retex)
    retex.set_defaults(func=cmd_retexture)

    remesh = sub.add_parser("remesh", help="retopologize / decimate an existing model")
    remesh.add_argument("--input-task-id")
    remesh.add_argument("--model-url")
    remesh.add_argument("--topology", choices=["quad", "triangle"])
    remesh.add_argument("--target-polycount", type=int)
    remesh.add_argument("--decimation-mode", type=int, choices=[1, 2, 3, 4])
    remesh.add_argument("--target-formats")
    add_runtime_args(remesh)
    remesh.set_defaults(func=cmd_remesh)

    rig = sub.add_parser("rig", help="auto-rig a humanoid model")
    rig.add_argument("--input-task-id")
    rig.add_argument("--model-url", help="public URL or local .glb path")
    rig.add_argument("--height-meters", type=float)
    rig.add_argument("--texture-image")
    add_runtime_args(rig)
    rig.set_defaults(func=cmd_rig)

    animate = sub.add_parser("animate", help="apply library or generated motion to a rig task")
    animate.add_argument("--rig-task-id", required=True)
    animate.add_argument("--animations", help="comma list of action ids or names (idle,walking,running)")
    animate.add_argument("--motion-task-id", help="text-to-motion task id instead of a library action")
    animate.add_argument("--fps", type=int, choices=[24, 25, 30, 60])
    add_runtime_args(animate)
    animate.set_defaults(func=cmd_animate)

    motion = sub.add_parser("motion", help="text to motion (custom clip)")
    motion.add_argument("--prompt", required=True)
    motion.add_argument("--duration", type=float, default=4.0, help="2-10s in 0.5 steps")
    motion.add_argument("--mode", choices=["prime", "swift"], default="prime")
    add_runtime_args(motion)
    motion.set_defaults(func=cmd_motion)

    status = sub.add_parser("status", help="show a task")
    status.add_argument("task_id")
    status.add_argument("--kind", choices=list(ENDPOINTS))
    status.add_argument("--api-key")
    status.set_defaults(func=cmd_status)

    download = sub.add_parser("download", help="download a succeeded task's outputs")
    download.add_argument("task_id")
    download.add_argument("--kind", choices=list(ENDPOINTS))
    download.add_argument("--api-key")
    download.add_argument("--name")
    download.add_argument("--out-dir", default="meshy-output")
    download.add_argument("--download-formats", default="glb")
    download.add_argument("--all-formats", action="store_true")
    download.set_defaults(func=cmd_download)

    balance = sub.add_parser("balance", help="show remaining credits")
    balance.add_argument("--api-key")
    balance.set_defaults(func=cmd_balance)

    listanim = sub.add_parser("list-animations", help="search the bundled animation library")
    listanim.add_argument("--search")
    listanim.add_argument("--category")
    listanim.add_argument("--limit", type=int, default=40)
    listanim.set_defaults(func=cmd_list_animations)

    vrig = sub.add_parser("validate-rig", help="check a downloaded rig GLB skeleton")
    vrig.add_argument("glb_path")
    vrig.set_defaults(func=cmd_validate_rig)

    vanim = sub.add_parser("validate-animation", help="keyframe QA for a downloaded clip GLB")
    vanim.add_argument("glb_path")
    vanim.set_defaults(func=cmd_validate_animation)

    pipeline = sub.add_parser("character-pipeline", help="generate -> texture -> rig -> animate -> download")
    pipeline.add_argument("--prompt")
    pipeline.add_argument("--model-task-id", help="resume from an existing textured task")
    pipeline.add_argument("--animations", default="idle,walking,running")
    pipeline.add_argument("--height-meters", type=float, default=1.7)
    pipeline.add_argument("--fps", type=int, choices=[24, 25, 30, 60])
    pipeline.add_argument("--pose-mode", choices=["a-pose", "t-pose", "empty"], default="t-pose")
    pipeline.add_argument("--art-style", choices=["realistic", "sculpture"])
    pipeline.add_argument("--negative-prompt")
    pipeline.add_argument("--seed", type=int)
    pipeline.add_argument("--no-refine", action="store_true")
    add_geometry_args(pipeline)
    pipeline.set_defaults(target_polycount=30000)
    add_texture_args(pipeline)
    add_runtime_args(pipeline, default_out="meshy-character")
    pipeline.set_defaults(func=cmd_character_pipeline)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        args.func(args)
    except MeshyError as exc:
        eprint(f"error: {exc}")
        return 1
    except KeyboardInterrupt:
        eprint("interrupted")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
