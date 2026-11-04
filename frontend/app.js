import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { STLLoader } from "three/addons/loaders/STLLoader.js";
import { mergeVertices } from "three/addons/utils/BufferGeometryUtils.js";

const $ = (id) => document.getElementById(id);
const status = (msg, err = false) => {
  $("status").textContent = msg;
  $("status").classList.toggle("error", err);
};

let imgId = null;
let lastStl = null;
let lastFile = null;

// ---- three.js viewer ----
const canvas = $("canvas");
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0d1117);
const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 5000);
camera.position.set(120, -160, 120);
const controls = new OrbitControls(camera, canvas);
controls.enableDamping = true;

scene.add(new THREE.HemisphereLight(0xffffff, 0x445566, 1.2));
const dir = new THREE.DirectionalLight(0xffffff, 1.5);
dir.position.set(80, 120, 60);
scene.add(dir);
scene.add(new THREE.GridHelper(300, 30, 0x30363d, 0x21262d));

let meshObj = null;
const material = new THREE.MeshStandardMaterial({ color: 0x58a6ff });

function showStl(buffer) {
  // STLLoader devuelve geometría no indexada -> fusionar vértices idénticos
  // para que computeVertexNormals produzca sombreado suave real
  let geom = new STLLoader().parse(buffer);
  geom = mergeVertices(geom, 1e-4);
  geom.computeVertexNormals();
  if (meshObj) {
    scene.remove(meshObj);
    meshObj.geometry.dispose();
  }
  meshObj = new THREE.Mesh(geom, material);
  scene.add(meshObj);
  geom.computeBoundingBox();
  const c = new THREE.Vector3();
  geom.boundingBox.getCenter(c);
  controls.target.copy(c);
  const size = geom.boundingBox.getSize(new THREE.Vector3()).length();
  camera.position.set(c.x + size, c.y - size * 1.3, c.z + size);
  controls.update();
}

function resize() {
  const { clientWidth: w, clientHeight: h } = canvas.parentElement;
  if (canvas.width !== w || canvas.height !== h) {
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  }
}
(function loop() {
  resize();
  controls.update();
  renderer.render(scene, camera);
  requestAnimationFrame(loop);
})();

// ---- form params ----
const vectorParams = () => {
  const fd = new FormData();
  for (const k of ["threshold", "threshold_hi", "turdsize", "opttolerance"]) {
    fd.append(k, $(k).value);
  }
  fd.append("alphamax", "1.0");
  fd.append("invert", $("invert").checked);
  return fd;
};

const stlParams = () => {
  const fd = vectorParams();
  fd.append("id", imgId);
  for (const k of ["height_mm", "width_mm", "bevel_mm", "bevel_scale", "base_mm", "base_margin_mm"]) {
    fd.append(k, $(k).value);
  }
  return fd;
};

for (const el of document.querySelectorAll('input[type="range"]')) {
  const out = $("o-" + el.id);
  if (out) el.addEventListener("input", () => (out.textContent = el.value));
}

// ---- API calls ----
async function prepare(file) {
  lastFile = file;
  const luma = $("luma").checked;
  status(luma ? "Vectorizando por luminancia…" : "Quitando fondo y vectorizando…");
  $("download").disabled = true;
  const fd = vectorParams();
  fd.append("skip_rembg", luma);
  fd.append("file", file);
  try {
    const r = await fetch("/api/prepare", { method: "POST", body: fd });
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
    const data = await r.json();
    imgId = data.id;
    if (data.mask_source === "alpha") {
      // máscara = canal alfa: umbral tipo rembg
      $("threshold").value = 128;
      $("threshold_hi").value = 255;
      $("invert").checked = false;
      Object.assign(modeParams.luma, { threshold: 128, threshold_hi: 255, invert: false });
      for (const k of ["threshold", "threshold_hi"]) $("o-" + k).textContent = $(k).value;
      status("Máscara desde canal alfa. Generando 3D…");
    } else {
      status("Listo. Generando 3D…");
    }
    $("maskImg").src = "data:image/png;base64," + data.mask_png;
    $("maskImg").hidden = false;
    $("svgBox").innerHTML = data.svg;
    await regenerate();
  } catch (e) {
    status("Error: " + e.message, true);
  }
}

async function regenerate() {
  if (!imgId) return;
  status("Generando STL…");
  $("download").disabled = true;
  try {
    const r = await fetch("/api/stl", { method: "POST", body: stlParams() });
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
    lastStl = await r.arrayBuffer();
    showStl(lastStl);
    $("download").disabled = false;
    status(`OK · ${r.headers.get("X-Vertex-Count")} vértices`);
  } catch (e) {
    status("Error: " + e.message, true);
  }
}

// ---- events ----
const drop = $("drop");
$("file").addEventListener("change", (e) => {
  const f = e.target.files[0];
  if (f) {
    $("previewImg").src = URL.createObjectURL(f);
    $("previewImg").hidden = false;
    $("dropText").hidden = true;
    prepare(f);
  }
});
drop.addEventListener("dragover", (e) => {
  e.preventDefault();
  drop.classList.add("over");
});
drop.addEventListener("dragleave", () => drop.classList.remove("over"));
drop.addEventListener("drop", (e) => {
  e.preventDefault();
  drop.classList.remove("over");
  const f = e.dataTransfer.files[0];
  if (f) {
    $("previewImg").src = URL.createObjectURL(f);
    $("previewImg").hidden = false;
    $("dropText").hidden = true;
    prepare(f);
  }
});

// cada modo recuerda sus ajustes de vectorización
const modeParams = {
  rembg: { threshold: 128, threshold_hi: 255, invert: false },
  luma: { threshold: 0, threshold_hi: 200, invert: false },
};

$("luma").addEventListener("change", () => {
  const luma = $("luma").checked;
  // guardar ajustes del modo que se abandona
  const prev = modeParams[luma ? "rembg" : "luma"];
  prev.threshold = +$("threshold").value;
  prev.threshold_hi = +$("threshold_hi").value;
  prev.invert = $("invert").checked;
  // restaurar ajustes del modo nuevo
  const next = modeParams[luma ? "luma" : "rembg"];
  $("threshold").value = next.threshold;
  $("threshold_hi").value = next.threshold_hi;
  $("invert").checked = next.invert;
  for (const k of ["threshold", "threshold_hi"]) $("o-" + k).textContent = $(k).value;
  if (lastFile) prepare(lastFile);
});

let debounce;
for (const k of ["threshold", "threshold_hi", "turdsize", "opttolerance", "invert", "height_mm", "width_mm", "bevel_mm", "bevel_scale", "base_mm", "base_margin_mm"]) {
  $(k).addEventListener("change", () => {
    clearTimeout(debounce);
    debounce = setTimeout(regenerate, 300);
  });
}
$("regen").addEventListener("click", regenerate);
$("download").addEventListener("click", () => {
  if (!lastStl) return;
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([lastStl]));
  a.download = "modelo.stl";
  a.click();
});
