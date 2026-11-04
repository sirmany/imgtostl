import base64
import io
import os
import secrets
import uuid

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from PIL import Image
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from . import pipeline

# Contraseña opcional: si APP_PASSWORD está definida, toda la app pide Basic Auth.
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")

app = FastAPI(title="imgtostl")


class BasicAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if not APP_PASSWORD:
            return await call_next(request)
        ok = False
        auth = request.headers.get("authorization", "")
        if auth.startswith("Basic "):
            try:
                _, pwd = base64.b64decode(auth[6:]).decode().split(":", 1)
                ok = secrets.compare_digest(pwd, APP_PASSWORD)
            except Exception:
                pass
        if not ok:
            return PlainTextResponse(
                "Autenticación requerida",
                401,
                {"WWW-Authenticate": 'Basic realm="imgtostl"'},
            )
        return await call_next(request)


app.add_middleware(BasicAuthMiddleware)

# Cache en memoria: id -> máscara PIL (L)
_masks: dict[str, object] = {}
MAX_CACHE = 32


@app.post("/api/prepare")
async def prepare(
    file: UploadFile = File(...),
    threshold: int = Form(128),
    turdsize: int = Form(8),
    alphamax: float = Form(1.0),
    opttolerance: float = Form(0.3),
    invert: bool = Form(False),
    skip_rembg: bool = Form(False),
    threshold_hi: int = Form(255),
):
    data = await file.read()
    if not data:
        raise HTTPException(400, "Archivo vacío")
    try:
        mask_source = "rembg"
        layers = None
        if skip_rembg:
            im = Image.open(io.BytesIO(data))
            alpha = im.split()[-1] if im.mode in ("RGBA", "LA") else None
            # usar el canal alfa si contiene transparencia real
            if alpha is not None and alpha.getextrema()[0] < 250:
                mask = alpha
                mask_source = "alpha"
            else:
                mask = im.convert("L")
                mask_source = "luma"
                layers = pipeline.color_masks(im)
                if layers:
                    import numpy as np

                    # máscara combinada: unión de todos los clusters
                    comb = np.zeros(np.array(mask).shape, bool)
                    for l in layers:
                        comb |= np.array(l["mask"]) > 0
                    mask = Image.fromarray(comb.astype(np.uint8) * 255)
        else:
            mask = pipeline.remove_background(data)

        if mask_source == "alpha":
            # el alfa es una máscara lista: umbral tipo rembg
            svg = pipeline.vectorize(mask, 128, turdsize, alphamax, opttolerance, invert, 255)
        else:
            svg = pipeline.vectorize(
                mask, threshold, turdsize, alphamax, opttolerance, invert, threshold_hi
            )
    except Exception as e:
        raise HTTPException(422, f"Error procesando imagen: {e}")

    if len(_masks) >= MAX_CACHE:
        _masks.pop(next(iter(_masks)))
    img_id = uuid.uuid4().hex
    _masks[img_id] = {"mask": mask, "layers": layers}
    return JSONResponse(
        {
            "id": img_id,
            "mask_png": pipeline.mask_png_b64(mask),
            "svg": svg,
            "mask_source": mask_source,
            "palette": (
                [{"idx": l["idx"], "color": l["color"], "count": l["count"]} for l in layers]
                if layers
                else None
            ),
        }
    )


def _get_mask(img_id: str):
    entry = _masks.get(img_id)
    if entry is None:
        raise HTTPException(404, "Imagen no encontrada, vuelve a subirla")
    return entry["mask"]


@app.post("/api/svg")
async def get_svg(
    id: str = Form(...),
    threshold: int = Form(128),
    turdsize: int = Form(8),
    alphamax: float = Form(1.0),
    opttolerance: float = Form(0.3),
    invert: bool = Form(False),
    threshold_hi: int = Form(255),
):
    svg = pipeline.vectorize(
        _get_mask(id), threshold, turdsize, alphamax, opttolerance, invert, threshold_hi
    )
    return Response(svg, media_type="image/svg+xml")


@app.post("/api/stl")
async def get_stl(
    id: str = Form(...),
    threshold: int = Form(128),
    turdsize: int = Form(8),
    alphamax: float = Form(1.0),
    opttolerance: float = Form(0.3),
    height_mm: float = Form(5.0),
    width_mm: float = Form(100.0),
    bevel_mm: float = Form(0.0),
    bevel_scale: float = Form(0.85),
    base_mm: float = Form(0.0),
    base_margin_mm: float = Form(2.0),
    invert: bool = Form(False),
    threshold_hi: int = Form(255),
    color_indices: str = Form(""),
):
    try:
        entry = _masks.get(id)
        if entry is None:
            raise HTTPException(404, "Imagen no encontrada, vuelve a subirla")
        mask, layers = entry["mask"], entry["layers"]

        # modo capas por color: indices de clusters seleccionados
        sel = [int(x) for x in color_indices.split(",") if x.strip().isdigit()]
        if layers and sel:
            order = [l for l in layers if l["idx"] in sel]  # oscuro->claro
            # escala común para todas las capas: la de la silueta combinada
            svg0 = pipeline.vectorize(
                mask, threshold, turdsize, alphamax, opttolerance, invert, threshold_hi
            )
            sx0, sy0 = pipeline._svg_to_path2d(svg0).extents
            scale0 = width_mm / float(max(sx0, sy0))
            solids = []
            z = 0.0
            if base_mm > 0:
                solids.append(
                    pipeline.extrude(svg0, base_mm, width_mm, anchor=False, scale_override=scale0)
                )
                z = base_mm
            EPS = 0.05  # interpenetración entre capas: unión volumétrica sin caras coplanares
            # bandas acumulativas: cada capa cubre su color + todos los superiores,
            # así cada banda está soportada por la anterior (imprimible)
            import numpy as np

            band = np.zeros(np.array(mask).shape, bool)
            band_masks = [None] * len(order)
            for j in range(len(order) - 1, -1, -1):
                band |= np.array(order[j]["mask"]) > 0
                band_masks[j] = band.copy()
            for j in range(len(order)):
                svg_l = pipeline.vectorize(
                    Image.fromarray(band_masks[j].astype(np.uint8) * 255),
                    128, turdsize, alphamax, opttolerance, invert, 255,
                )
                solids.append(
                    pipeline.extrude(
                        svg_l, height_mm + EPS, width_mm, anchor=False,
                        z_shift_mm=z - EPS, scale_override=scale0,
                    )
                )
                z += height_mm
            mesh = pipeline.unite(solids)
        else:
            svg = pipeline.vectorize(
                mask, threshold, turdsize, alphamax, opttolerance, invert, threshold_hi
            )
            mesh = pipeline.extrude(
                svg, height_mm, width_mm, bevel_mm, bevel_scale, base_mm, base_margin_mm
            )
    except Exception as e:
        raise HTTPException(422, f"Error generando STL: {e}")

    return Response(
        pipeline.stl_bytes(mesh),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": "attachment; filename=modelo.stl",
            "X-Vertex-Count": str(len(mesh.vertices)),
        },
    )


# Frontend estático (se monta al final para no pisar /api)
import os

_static = os.path.join(os.path.dirname(__file__), "..", "frontend")
app.mount("/", StaticFiles(directory=os.path.abspath(_static), html=True))
