# imgtostl

Web app: imagen → quitar fondo → vectorizar → extruir a 3D → STL.

## Requisitos

- Python 3.10+
- `potrace` instalado en el sistema (`sudo apt install potrace`)

## Instalación

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Ejecutar

```bash
.venv/bin/uvicorn backend.main:app --reload
```

Abrir http://localhost:8000

La primera conversión descarga el modelo u2net (~170MB) en `~/.rembg/models/`.

## Pipeline

1. `rembg` (u2net) → máscara alfa del objeto
2. `potrace` → SVG del contorno
3. Loft manual con trimesh → STL watertight (tapas con `mapbox_earcut`, agujeros soportados)

## Parámetros

| Parámetro | Efecto |
|---|---|
| Umbral | Binarización de la máscara alfa |
| Detalle mínimo | `potrace -t` (suprime speckles) |
| Suavizado | `potrace -O` (tolerancia de optimización de curvas) |
| Altura / Ancho | Dimensiones del STL en mm |
| Bisel / Factor | Altura de la última etapa y escala superior (taper) |
