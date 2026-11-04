"""Pipeline: imagen -> sin fondo -> máscara -> SVG (potrace) -> STL (loft/extrusión)."""
import base64
import io
import os
import subprocess
import tempfile

import numpy as np
import trimesh
from PIL import Image, ImageOps
from rembg import new_session, remove
from shapely.affinity import scale as shp_scale
from shapely.ops import unary_union
from trimesh.creation import extrude_polygon, triangulate_polygon

_session = None


def get_session():
    global _session
    if _session is None:
        _session = new_session("u2net")
    return _session


def remove_background(image_bytes: bytes) -> Image.Image:
    """Devuelve máscara en modo L: 255 = objeto, 0 = fondo."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    out = remove(img, session=get_session())
    return out.split()[-1]


def mask_png_b64(mask: Image.Image) -> str:
    buf = io.BytesIO()
    mask.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


def vectorize(
    mask: Image.Image,
    threshold: int = 128,
    turdsize: int = 8,
    alphamax: float = 1.0,
    opttolerance: float = 0.3,
    invert: bool = False,
    threshold_hi: int = 255,
) -> str:
    """Máscara L -> bitmap -> potrace -> SVG string.

    Objeto = píxeles con valor en [threshold, threshold_hi].
    - rembg: threshold=128, hi=255 (alfa alto = objeto)
    - luminancia oscuro sobre claro: threshold=0, hi=200
    - luminancia claro sobre oscuro: threshold=200, hi=255
    """
    # potrace: negro = objeto; invert=True invierte la máscara
    lo, hi = threshold, threshold_hi
    if invert:
        bw = mask.point(lambda v: 255 if lo <= v <= hi else 0)
    else:
        bw = mask.point(lambda v: 0 if lo <= v <= hi else 255)
    bw = bw.convert("1", dither=Image.Dither.NONE)
    # margen: evita que potrace recorte formas que tocan el borde (p.ej. ®)
    bw = ImageOps.expand(bw, border=8, fill=1)

    with tempfile.TemporaryDirectory() as td:
        pbm = os.path.join(td, "in.pbm")
        svg = os.path.join(td, "out.svg")
        bw.save(pbm)
        subprocess.run(
            [
                "potrace",
                "-s",
                "--flat",
                "-t",
                str(turdsize),
                "-a",
                str(alphamax),
                "-O",
                str(opttolerance),
                "-o",
                svg,
                pbm,
            ],
            check=True,
            capture_output=True,
        )
        with open(svg) as f:
            return f.read()


def _svg_to_path2d(svg_str: str):
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "in.svg")
        with open(p, "w") as f:
            f.write(svg_str)
        loaded = trimesh.load_path(p)
    if hasattr(loaded, "polygons_full"):
        return loaded
    # Scene -> merge geometry into a Path2D
    if hasattr(loaded, "geometry"):
        path = trimesh.path.Path2D(
            entities=[e for g in loaded.geometry.values() for e in g.entities],
            vertices=np.vstack([g.vertices for g in loaded.geometry.values()]),
        )
        return path
    raise ValueError("No se pudo interpretar el SVG")


def _loft_polygon(poly, stages):
    """Extruye un polígono (con agujeros) siguiendo stages=[(scale, z), ...].

    Todas las etapas comparten el mismo anillo de vértices (escala afín),
    así que el loft es trivial y watertight.
    """
    def _clean(ring):
        pts = np.array(ring)[:-1]  # quitar punto de cierre
        keep = np.ones(len(pts), bool)
        keep[1:] = np.any(np.diff(pts, axis=0) != 0, axis=1)
        return pts[keep]

    rings = [_clean(poly.exterior.coords)]
    rings += [_clean(r.coords) for r in poly.interiors]
    if len(rings[0]) < 3:
        return [], []
    rings = [r for r in rings if len(r) >= 3]

    verts, faces = [], []
    for ring in rings:
        n = len(ring)
        base = len(verts)
        for s, z in stages:
            for x, y in ring:
                verts.append([x * s, y * s, z])
        for st in range(len(stages) - 1):
            a = base + st * n
            b = base + (st + 1) * n
            for i in range(n):
                j = (i + 1) % n
                faces.append([a + i, a + j, b + j])
                faces.append([a + i, b + j, b + i])

    # Tapas: triangulación con agujeros
    s0, z0 = stages[0]
    s1, z1 = stages[-1]
    v2d, f = triangulate_polygon(shp_scale(poly, xfact=s0, yfact=s0, origin=(0, 0)))
    off = len(verts)
    verts += [[x, y, z0] for x, y in v2d]
    faces += [[off + i0, off + i2, off + i1] for i0, i1, i2 in f]  # normal -Z
    v2d, f = triangulate_polygon(shp_scale(poly, xfact=s1, yfact=s1, origin=(0, 0)))
    off = len(verts)
    verts += [[x, y, z1] for x, y in v2d]
    faces += [[off + i for i in tri] for tri in f]  # normal +Z

    return verts, faces


def color_masks(img: Image.Image, n_colors: int = 6):
    """Cuantiza la imagen RGB en clusters de color.

    Devuelve lista ordenada oscuro->claro de dicts
    {idx, color('#rrggbb'), count, mask (PIL L: 255=cluster)}.
    """
    q = img.convert("RGB").quantize(colors=n_colors * 2, method=Image.Quantize.MEDIANCUT)
    pal = q.getpalette()
    labels = np.array(q)
    total = labels.size
    raw = []
    for i in range(n_colors * 2):
        sel = labels == i
        cnt = int(sel.sum())
        if cnt < total * 0.005:  # ignorar clusters ínfimos
            continue
        r, g, b = pal[i * 3 : i * 3 + 3]
        raw.append({"rgb": np.array([r, g, b], float), "count": cnt, "sel": sel})

    # fusionar clusters cuyo color es casi igual (blancos/grises adyacentes)
    merged = []
    for c in raw:
        target = None
        for m in merged:
            if np.linalg.norm(m["rgb"] - c["rgb"]) < 36:
                target = m
                break
        if target is None:
            merged.append(dict(c))
        else:
            w = target["count"] + c["count"]
            target["rgb"] = (target["rgb"] * target["count"] + c["rgb"] * c["count"]) / w
            target["count"] = w
            target["sel"] = target["sel"] | c["sel"]

    out = []
    for i, m in enumerate(merged):
        r, g, b = (int(v) for v in m["rgb"])
        out.append(
            {
                "idx": i,
                "color": "#%02x%02x%02x" % (r, g, b),
                "lum": 0.299 * r + 0.587 * g + 0.114 * b,
                "count": m["count"],
                "mask": Image.fromarray((m["sel"] * 255).astype(np.uint8)),
            }
        )
    out.sort(key=lambda d: d["lum"])  # oscuro abajo -> claro arriba
    return out


def extrude(
    svg_str: str,
    height_mm: float = 5.0,
    width_mm: float = 100.0,
    bevel_mm: float = 0.0,
    bevel_scale: float = 0.85,
    base_mm: float = 0.0,
    base_margin_mm: float = 2.0,
    z_shift_mm: float = 0.0,
    anchor: bool = True,
    scale_override: float = None,
) -> trimesh.Trimesh:
    path = _svg_to_path2d(svg_str)
    if len(path.polygons_full) == 0:
        raise ValueError("El SVG no contiene formas")
    sx, sy = path.extents
    scale = scale_override or width_mm / float(max(sx, sy))

    z_off = max(0.0, base_mm)
    b = max(0.0, min(bevel_mm, height_mm * 0.49))
    top = max(0.05, min(bevel_scale, 1.0))

    scaled = []
    for p in path.polygons_full:
        if p.area < 1e-6:
            continue
        # yfact=-scale: potrace usa Y hacia abajo; invertir para lectura desde +Z
        p = shp_scale(p, xfact=scale, yfact=-scale, origin=(0, 0))
        # buffer(0): spikes/auto-intersecciones; simplify: colineales patológicos
        p = p.buffer(0).simplify(0.01, preserve_topology=True)
        scaled += [g for g in getattr(p, "geoms", [p]) if g.area > 1e-6]

    solids = []

    # Placa base: unión de las formas dilatadas, extruida de 0 a base_mm
    if z_off > 0:
        base_geom = unary_union([p.buffer(base_margin_mm) for p in scaled])
        for bp in getattr(base_geom, "geoms", [base_geom]):
            if bp.area > 1e-6:
                solids.append(extrude_polygon(bp, z_off))

    for poly in scaled:
        if b > 0 and top < 1.0:
            body = extrude_polygon(poly, height_mm - b)
            body.apply_translation([0, 0, z_off])
            solids.append(body)
            v, f = _loft_polygon(poly, [(1.0, 0.0), (top, b)])
            band = trimesh.Trimesh(
                vertices=np.array(v, dtype=np.float64),
                faces=np.array(f, dtype=np.int64),
                process=True,
            )
            band.apply_translation([0, 0, z_off + height_mm - b])
            solids.append(band)
        else:
            body = extrude_polygon(poly, height_mm)
            body.apply_translation([0, 0, z_off])
            solids.append(body)

    if len(solids) == 1:
        mesh = solids[0]
    else:
        try:
            mesh = trimesh.boolean.union(solids, engine="manifold")
        except Exception:
            # fallback: concatenar (multi-shell, slicers lo aceptan)
            mesh = trimesh.util.concatenate(solids)
            trimesh.repair.fix_normals(mesh)
    if z_shift_mm:
        mesh.apply_translation([0, 0, z_shift_mm])
    if anchor:
        # anclar: esquina mínima en el origen
        mesh.apply_translation(-mesh.bounds[0])
    return mesh


def unite(solids):
    """Une una lista de mallas (manifold si es posible) y ancla al origen."""
    if len(solids) == 1:
        mesh = solids[0]
    else:
        try:
            mesh = trimesh.boolean.union(solids, engine="manifold")
        except Exception as e:
            import logging

            logging.getLogger("imgtostl").warning("union manifold falló: %r", e)
            mesh = trimesh.util.concatenate(solids)
            trimesh.repair.fix_normals(mesh)
    mesh.apply_translation(-mesh.bounds[0])
    return mesh


def stl_bytes(mesh: trimesh.Trimesh) -> bytes:
    return mesh.export(file_type="stl")
