"""Smart 9:16 framing: find the streamer's face and choose a layout.

- split : face-cam overlay on top of gameplay  -> cam on top, gameplay below (classic clip layout)
- crop  : big / moving face (IRL, just chatting) -> full-height 9:16 crop that follows the face
- blur  : no usable face -> whole frame on a blurred background
"""

import subprocess

import numpy as np

from .config import FFMPEG, MODELS_DIR

YUNET = MODELS_DIR / "face" / "face_detection_yunet_2023mar.onnx"
OUT_W, OUT_H = 1080, 1920
TITLE_H = 300                   # split layout: title band on top, so the title never covers the face
CAM_H = 660                     # split layout: face-cam height, gameplay gets the rest
GAME_H = OUT_H - TITLE_H - CAM_H
SAMPLE_FPS = 2.0
ANALYSIS_W = 640


def _frames(src: str, start: float, duration: float, src_w: int, src_h: int):
    w = ANALYSIS_W
    h = int(round(src_h * w / src_w / 2) * 2)
    cmd = [FFMPEG, "-nostdin", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(src),
           "-vf", f"fps={SAMPLE_FPS},scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    raw = subprocess.run(cmd, capture_output=True).stdout
    n = len(raw) // (w * h * 3)
    return np.frombuffer(raw[: n * w * h * 3], np.uint8).reshape(n, h, w, 3), w, h


def _detect(frames, w, h) -> list[list[tuple]]:
    import cv2

    det = cv2.FaceDetectorYN.create(str(YUNET), "", (w, h), 0.72, 0.3, 50)
    out = []
    for f in frames:
        _, faces = det.detect(np.ascontiguousarray(f))
        found = []
        for row in faces if faces is not None else []:
            x, y, fw, fh, score = float(row[0]), float(row[1]), float(row[2]), float(row[3]), float(row[-1])
            if fh < h * 0.035 or score < 0.72:
                continue
            found.append(((x + fw / 2) / w, (y + fh / 2) / h, fw / w, fh / h, score))
        out.append(found)
    return out


def _clusters(dets: list[list[tuple]]) -> list[dict]:
    clusters: list[dict] = []
    for fi, faces in enumerate(dets):
        for cx, cy, fw, fh, sc in faces:
            best = None
            for c in clusters:
                d = np.hypot(cx - c["cx"], cy - c["cy"])
                ratio = fh / c["fh"]
                if d < 0.07 and 0.6 < ratio < 1.6 and (best is None or d < best[0]):
                    best = (d, c)
            if best is None:
                clusters.append({"cx": cx, "cy": cy, "fw": fw, "fh": fh, "items": [(fi, cx, cy, fw, fh)]})
            else:
                c = best[1]
                c["items"].append((fi, cx, cy, fw, fh))
                arr = np.array([it[1:] for it in c["items"]])
                c["cx"], c["cy"], c["fw"], c["fh"] = (float(v) for v in np.median(arr, axis=0))
    n = max(1, len(dets))
    for c in clusters:
        arr = np.array([it[1:3] for it in c["items"]])
        c["presence"] = len({it[0] for it in c["items"]}) / n
        c["jitter"] = float(np.max(np.std(arr, axis=0))) if len(arr) > 1 else 0.0
    return sorted(clusters, key=lambda c: c["presence"] * (0.5 + c["fh"]), reverse=True)


def _even(v: float) -> int:
    return int(round(v / 2) * 2)


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _overlay_rect(frames, c: dict, w: int, h: int) -> tuple[float, float, float, float] | None:
    """Find the face-cam overlay borders: static straight edges around the face in the temporal median frame."""
    if len(frames) < 4:
        return None
    pick = frames[:: max(1, len(frames) // 24)]
    gray = np.median(pick.mean(axis=3), axis=0).astype(np.float32)
    gx = np.abs(np.diff(gray, axis=1))
    gy = np.abs(np.diff(gray, axis=0))
    fx, fy, fw, fh = c["cx"] * w, c["cy"] * h, c["fw"] * w, c["fh"] * h
    y0, y1 = int(_clamp(fy - fh, 0, h - 2)), int(_clamp(fy + fh, 1, h - 1))
    x0, x1 = int(_clamp(fx - fw, 0, w - 2)), int(_clamp(fx + fw, 1, w - 1))

    def best(profile: np.ndarray, lo: int, hi: int):
        lo, hi = max(0, lo), min(len(profile), hi)
        if hi - lo < 3:
            return None
        seg = profile[lo:hi]
        i = int(np.argmax(seg))
        if seg[i] >= 12 and seg[i] >= 3.0 * (np.median(seg) + 1.0):
            return lo + i
        return None

    col = gx[y0:y1].mean(axis=0)
    row = gy[:, x0:x1].mean(axis=1)
    right = best(col, int(fx + 0.7 * fw), int(fx + 4.0 * fw))
    left = best(col, int(fx - 4.0 * fw), int(fx - 0.7 * fw))
    top = best(row, int(fy - 3.2 * fh), int(fy - 0.8 * fh))
    bottom = best(row, int(fy + 0.8 * fh), int(fy + 3.2 * fh))
    found = sum(v is not None for v in (left, right, top, bottom))
    if found < 2:
        return None
    # A missing side means the cam touches the frame edge (or has no visible border) on that side.
    L = (left + 1) if left is not None else (0 if fx - 4.0 * fw <= 0 else fx - 2.2 * fw)
    R = right if right is not None else (w if fx + 4.0 * fw >= w else fx + 2.2 * fw)
    T = (top + 1) if top is not None else (0 if fy - 3.2 * fh <= 0 else fy - 1.8 * fh)
    B = bottom if bottom is not None else (h if fy + 3.2 * fh >= h else fy + 1.8 * fh)
    if R - L < 1.6 * fw or B - T < 1.6 * fh:
        return None
    return L / w, T / h, R / w, B / h


def _split_boxes(c: dict, W: int, H: int, rect: tuple | None = None, cam_h: int = CAM_H) -> dict:
    fx, fy, fh = c["cx"] * W, c["cy"] * H, c["fh"] * H
    cam_aspect = OUT_W / cam_h
    cam = None
    if rect is not None:
        L, T, R, B = rect[0] * W, rect[1] * H, rect[2] * W, rect[3] * H
        rw, rh = R - L, B - T
        if rw / rh > cam_aspect:
            ch, cw = rh, rh * cam_aspect
        else:
            cw, ch = rw, rw / cam_aspect
        if ch >= fh * 1.5:
            cx = _clamp(fx - cw / 2, L, R - cw)
            cy = _clamp(fy + 0.1 * fh - ch / 2, T, B - ch)
            cam = (_even(cw), _even(ch), _even(cx), _even(cy))
    if cam is None:
        ch = _clamp(max(fh * 2.4, H * 0.22), 64, H)
        cw = min(W, ch * cam_aspect)
        ch = cw / cam_aspect
        cx = _clamp(fx - cw / 2, 0, W - cw)
        cy = _clamp(fy + 0.1 * fh - ch / 2, 0, H - ch)
        cam = (_even(cw), _even(ch), _even(cx), _even(cy))
    cw, ch, cx, cy = cam

    game_aspect = OUT_W / (OUT_H - TITLE_H - cam_h)
    gh = H
    gw = min(W, gh * game_aspect)
    gx = (W - gw) / 2
    cam_l, cam_r = cx, cx + cw
    # Keep the face-cam out of the gameplay crop (a second, cut-off cam looks broken).
    if cam_r <= W * 0.5:            # cam on the left -> gameplay crop starts right of it
        gx = _clamp(max(gx, cam_r), 0, W - gw)
    elif cam_l >= W * 0.5:
        gx = _clamp(min(gx, cam_l - gw), 0, W - gw)
    game = (_even(gw), _even(gh), _even(gx), 0)
    return {"cam": cam, "game": game, "cam_h": cam_h}


def _region_boxes(c: dict, W: int, H: int, region: list) -> dict | None:
    """Streamer-defined cam area (cam + overlays such as a live subscriber counter), in 1920x1080 pixels.

    Used only when the detected face really sits inside it; the cam panel gets the height that keeps its aspect.
    """
    x0, y0, x1, y1 = (float(v) for v in region)
    sx, sy = W / 1920.0, H / 1080.0
    L, T, R, B = x0 * sx, y0 * sy, x1 * sx, y1 * sy
    if R - L < 32 or B - T < 32 or not (L <= c["cx"] * W <= R and T <= c["cy"] * H <= B):
        return None
    cam_h = int(_clamp(round(OUT_W * (B - T) / (R - L) / 2) * 2, 480, 940))
    need_h = (R - L) * cam_h / OUT_W
    if B - T > need_h + 2:  # region taller than the max panel: keep the middle part
        mid = (T + B) / 2
        T, B = mid - need_h / 2, mid + need_h / 2
    elif B - T < need_h - 2:  # region wider than the min panel: keep the part around the face
        need_w = (B - T) * OUT_W / cam_h
        L = _clamp(c["cx"] * W - need_w / 2, L, R - need_w)
        R = L + need_w
    boxes = _split_boxes(c, W, H, None, cam_h)
    boxes["cam"] = (_even(R - L), _even(B - T), _even(L), _even(T))
    boxes["fixed_cam"] = True
    return boxes


def _track(dets: list[list[tuple]], W: int, crop_w: int, anchor: float | None) -> list[tuple[float, float]]:
    """Smoothed, 'camera-operator' style horizontal crop path: holds still, glides when the face moves away."""
    n = len(dets)
    xs = np.full(n, np.nan)
    prev = anchor
    for i, faces in enumerate(dets):
        if not faces:
            continue
        if prev is None:
            f = max(faces, key=lambda f: f[3])
        else:
            f = max(faces, key=lambda f: f[3] * 2 - abs(f[0] - prev))
        xs[i] = f[0]
        prev = f[0]
    if np.all(np.isnan(xs)):
        return [(0.0, (W - crop_w) / 2)]
    # Cutaways without faces: hold the last framing instead of drifting.
    first = int(np.argmax(~np.isnan(xs)))
    xs[:first] = xs[first]
    for i in range(1, n):
        if np.isnan(xs[i]):
            xs[i] = xs[i - 1]
    k = 2
    med = np.array([np.median(xs[max(0, i - k): i + k + 1]) for i in range(n)])
    dead = 0.12 * crop_w / W
    cur = med[0]
    path = []
    for i in range(n):
        if abs(med[i] - cur) > dead:
            cur += (med[i] - cur) * 0.55
        path.append(cur)
    lefts = [_clamp(x * W - crop_w / 2, 0, W - crop_w) for x in path]
    keys = []
    for i, left in enumerate(lefts):
        moving_in = i > 0 and abs(left - lefts[i - 1]) > 1
        moving_out = i + 1 < n and abs(lefts[i + 1] - left) > 1
        if i == 0 or i == n - 1 or moving_in or moving_out:
            keys.append((i / SAMPLE_FPS, float(left)))
    if len(keys) > 40:
        keys = keys[:: int(np.ceil(len(keys) / 40))] + [keys[-1]]
    return keys


def analyze(src: str, start: float, duration: float, src_w: int, src_h: int, prefer: str = "auto",
            cam_region: list | None = None) -> dict:
    base = {"layout": "blur", "src_w": src_w, "src_h": src_h}
    if prefer == "blur" or not YUNET.exists():
        return base
    try:
        frames, w, h = _frames(src, start, duration, src_w, src_h)
        if len(frames) == 0:
            return base
        dets = _detect(frames, w, h)
    except Exception:  # noqa: BLE001 - framing is best effort
        return base
    clusters = _clusters(dets)
    frac_with_face = sum(1 for d in dets if d) / max(1, len(dets))
    stable = [c for c in clusters if c["presence"] >= 0.35 and c["jitter"] < 0.035]
    W, H = src_w, src_h
    crop_w = _even(min(W, H * OUT_W / OUT_H))
    info = {**base, "faces": [{k: round(v, 3) for k, v in c.items() if k != "items"} for c in clusters[:3]],
            "face_frames": round(frac_with_face, 2)}

    main = stable[0] if stable else None
    if main is not None and cam_region and prefer in ("auto", "split"):
        boxes = _region_boxes(main, W, H, cam_region)
        if boxes is not None:
            return {**info, "layout": "split", **boxes}
    if main is not None:
        corner = abs(main["cx"] - 0.5) > 0.17 or abs(main["cy"] - 0.5) > 0.2
        if (prefer in ("auto", "split") and main["fh"] < 0.38 and corner) or prefer == "split":
            rect = _overlay_rect(frames, main, w, h)
            if rect is not None and (rect[2] - rect[0]) > 0.75 and prefer != "split":
                rect = None
            else:
                boxes = _split_boxes(main, W, H, rect)
                cw, _, cx, _ = boxes["cam"]
                gw, _, gx, _ = boxes["game"]
                overlap = max(0, min(cx + cw, gx + gw) - max(cx, gx))
                # If the "face-cam" sits inside the gameplay crop, the camera IS the content -> crop instead.
                if overlap <= 0.35 * cw or prefer == "split":
                    info["cam_rect"] = [round(v, 3) for v in rect] if rect else None
                    return {**info, "layout": "split", **boxes}
        big_stable = [c for c in stable if c["fh"] >= 0.1]
        if big_stable and prefer in ("auto", "crop"):
            xs = [c["cx"] for c in big_stable]
            if (max(xs) - min(xs)) * W <= crop_w * 0.75:
                center = (max(xs) + min(xs)) / 2
                left = _clamp(center * W - crop_w / 2, 0, W - crop_w)
                return {**info, "layout": "crop", "crop_w": crop_w, "track": [(0.0, float(left))]}
    if frac_with_face >= 0.5 and prefer in ("auto", "crop"):
        sizes = [max(f[3] for f in d) for d in dets if d]
        if np.median(sizes) >= 0.1:
            anchor = clusters[0]["cx"] if clusters else None
            return {**info, "layout": "crop", "crop_w": crop_w, "track": _track(dets, W, crop_w, anchor)}
    return info


def crop_x_expr(track: list[tuple[float, float]]) -> str:
    """Piecewise-linear ffmpeg expression of t for the crop's x offset."""
    if len(track) == 1:
        return str(int(track[0][1]))
    expr = str(int(track[-1][1]))
    for (t0, x0), (t1, x1) in reversed(list(zip(track, track[1:]))):
        if t1 - t0 <= 0:
            continue
        seg = f"{x0:.1f}+({x1 - x0:.1f})*(t-{t0:.2f})/{t1 - t0:.2f}"
        expr = f"if(lt(t,{t1:.2f}),{seg},{expr})"
    return f"if(lt(t,{track[0][0]:.2f}),{int(track[0][1])},{expr})"
