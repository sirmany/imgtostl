import base64
import io
import os
import secrets
import uuid

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from PIL import Image, ImageOps
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
    _masks[img_id] = {"mask": mask, "source": mask_source}
    return JSONResponse(
        {
            "id": img_id,
            "mask_png": pipeline.mask_png_b64(mask),
            "svg": svg,
            "mask_source": mask_source,
        }
    )


@app.get("/api/fonts")
async def fonts():
    return pipeline.list_fonts()


@app.post("/api/prepare_text")
async def prepare_text(
    text: str = Form(""),
    font: str = Form(""),
    size: int = Form(120),
    spacing: float = Form(0.0),
    stroke: int = Form(0),
    hollow: bool = Form(False),
    combine_id: str = Form(""),
    gap: int = Form(20),
    threshold: int = Form(0),
    turdsize: int = Form(8),
    alphamax: float = Form(1.0),
    opttolerance: float = Form(0.3),
    invert: bool = Form(False),
    threshold_hi: int = Form(200),
):
    if not text.strip():
        raise HTTPException(400, "Texto vacío")
    fonts_map = {}
    for f in pipeline.list_fonts():
        fonts_map.setdefault(f["name"], f["path"])  # empaquetadas primero
    path = fonts_map.get(font)
    if not path:
        raise HTTPException(400, f"Fuente desconocida: {font}")
    try:
        mask = pipeline.render_text(text, path, size, spacing, stroke, hollow)
        source = "text"

        # combinar: texto debajo de una máscara existente
        entry = _masks.get(combine_id) if combine_id else None
        if entry:
            base, bsrc = entry["mask"], entry["source"]
            bw, bh = base.size
            tw, th = mask.size
            W, H = max(bw, tw), bh + gap + th
            pos = ((W - tw) // 2, bh + gap)
            if bsrc in ("alpha", "rembg"):
                # semántica alfa: objeto=blanco
                canvas = Image.new("L", (W, H), 0)
                canvas.paste(base, (0, 0))
                canvas.paste(ImageOps.invert(mask), pos)
                source = bsrc
            else:
                # semántica luma/texto: objeto=negro
                canvas = Image.new("L", (W, H), 255)
                canvas.paste(base, (0, 0))
                canvas.paste(mask, pos)
                source = "luma"
            mask = canvas

        if source in ("alpha", "rembg"):
            svg = pipeline.vectorize(mask, 128, turdsize, alphamax, opttolerance, invert, 255)
        else:
            svg = pipeline.vectorize(
                mask, threshold, turdsize, alphamax, opttolerance, invert, threshold_hi
            )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(422, f"Error procesando texto: {e}")

    if len(_masks) >= MAX_CACHE:
        _masks.pop(next(iter(_masks)))
    img_id = uuid.uuid4().hex
    _masks[img_id] = {"mask": mask, "source": source}
    return JSONResponse(
        {
            "id": img_id,
            "mask_png": pipeline.mask_png_b64(mask),
            "svg": svg,
            "mask_source": source,
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
):
    try:
        svg = pipeline.vectorize(
            _get_mask(id), threshold, turdsize, alphamax, opttolerance, invert, threshold_hi
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
