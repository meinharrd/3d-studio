// three.js model viewer with display modes: wire, shaded (clay), textured (studio light), lights (model's own lights).
import * as THREE from 'three';
import {GLTFLoader} from 'three/addons/loaders/GLTFLoader.js';
import {OrbitControls} from 'three/addons/controls/OrbitControls.js';
import {RoomEnvironment} from 'three/addons/environments/RoomEnvironment.js';
import {EffectComposer} from 'three/addons/postprocessing/EffectComposer.js';
import {RenderPass} from 'three/addons/postprocessing/RenderPass.js';
import {UnrealBloomPass} from 'three/addons/postprocessing/UnrealBloomPass.js';
import {OutputPass} from 'three/addons/postprocessing/OutputPass.js';

// Blender's glTF exporter (lighting mode "SPEC") multiplies every light by 683 lm/W — sun included
// (W/m² → lux) — while three.js lights use Blender-like radiometric values. Undo it for all types.
const LIGHT_SCALE = 1 / 683;
const MODES = ['wire', 'shaded', 'textured', 'lights'];

const stage = document.getElementById('stage');
const poster = document.getElementById('poster');
const note = document.getElementById('lightNote');

const renderer = new THREE.WebGLRenderer({antialias: true, alpha: true});
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.toneMapping = THREE.NeutralToneMapping;
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFShadowMap;
stage.prepend(renderer.domElement);

const scene = new THREE.Scene();
const pmrem = new THREE.PMREMGenerator(renderer);
const envTex = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
const camera = new THREE.PerspectiveCamera(35, 1, 0.01, 1000);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.maxPolarAngle = Math.PI / 2 - 0.03;  // stay above ground: undersides are never meant to be seen
controls.autoRotate = true;
controls.autoRotateSpeed = 0.8;
controls.addEventListener('start', () => { controls.autoRotate = false; });

const key = new THREE.DirectionalLight(0xffffff, 1.6);
key.castShadow = true;
key.shadow.mapSize.set(2048, 2048);
key.shadow.bias = -0.0005;
key.shadow.normalBias = 0.02;
scene.add(key, key.target);
const ground = new THREE.Mesh(new THREE.PlaneGeometry(1, 1), new THREE.ShadowMaterial({opacity: 0.35, depthWrite: false, polygonOffset: true, polygonOffsetFactor: 4, polygonOffsetUnits: 4}));
ground.rotation.x = -Math.PI / 2;
ground.receiveShadow = true;
scene.add(ground);

const wireMat = new THREE.MeshBasicMaterial({color: 0xd8d4ca, wireframe: true, transparent: true, opacity: 0.55});
const clayMat = new THREE.MeshStandardMaterial({color: 0xa9a59c, roughness: 0.85, metalness: 0});

const composer = new EffectComposer(renderer);
composer.addPass(new RenderPass(scene, camera));
const bloom = new UnrealBloomPass(new THREE.Vector2(256, 256), 0.35, 0.4, 0.95);
composer.addPass(bloom);
composer.addPass(new OutputPass());

let root = null, meshes = [], lights = [], emissive = false, loadId = 0;
// animation: glTF clips play on a loop; brightness flicker is by naming convention ("flicker" in a
// light's or material's name), since three.js can't load animated light/emission values from glTF
let mixer = null, flickerLights = [], flickerMats = [], paused = false;
const clock = new THREE.Clock();
const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
const FLICKER = /flicker/i;
function flicker(t, seed) { return 0.8 + 0.12 * Math.sin(t * 13 + seed) + 0.08 * Math.sin(t * 29.7 + seed * 2); }
let mode = MODES.includes(localStorage.viewMode) ? localStorage.viewMode : 'textured';

function dispose(obj) {
  obj.traverse(o => {
    if (o.geometry) o.geometry.dispose();
    const mats = o.userData.orig ? [].concat(o.userData.orig) : [];
    for (const m of mats) {
      for (const v of Object.values(m)) if (v && v.isTexture) v.dispose();
      m.dispose();
    }
  });
}

function stopAnimation() {
  if (mixer) { mixer.stopAllAction(); if (root) mixer.uncacheRoot(root); mixer = null; }
  flickerLights = []; flickerMats = [];
}

function clear() {
  loadId++;
  stopAnimation();
  if (root) { scene.remove(root); dispose(root); }
  root = null; meshes = []; lights = [];
  showLoading(false); note.hidden = true;
}

function frame() {
  const box = new THREE.Box3();
  for (const m of meshes) box.expandByObject(m);
  if (box.isEmpty()) box.setFromCenterAndSize(new THREE.Vector3(), new THREE.Vector3(1, 1, 1));
  const center = box.getCenter(new THREE.Vector3());
  const r = Math.max(box.getSize(new THREE.Vector3()).length() / 2, 0.01);
  const dir = new THREE.Vector3(1, 0.55, 1.25).normalize();
  camera.position.copy(center).addScaledVector(dir, r / Math.sin(THREE.MathUtils.degToRad(camera.fov / 2)) * 1.05);
  camera.near = r / 20; camera.far = r * 40;  // tight range = more depth precision
  camera.updateProjectionMatrix();
  controls.target.copy(center);
  controls.minDistance = r * 0.3; controls.maxDistance = r * 10;
  controls.autoRotate = true;
  controls.update();
  key.position.copy(center).add(new THREE.Vector3(r * 1.5, r * 3, r * 1.2));
  key.target.position.copy(center);
  const sc = key.shadow.camera;
  sc.left = sc.bottom = -r * 1.6; sc.right = sc.top = r * 1.6; sc.near = 0.01; sc.far = r * 10;
  sc.updateProjectionMatrix();
  ground.position.set(center.x, box.min.y - r * 0.004, center.z);  // below the model's own floor: no z-fight
  ground.scale.setScalar(r * 8);
}

function applyMode() {
  const lit = mode === 'lights';
  for (const m of meshes) m.material = mode === 'wire' ? wireMat : mode === 'shaded' ? clayMat : m.userData.orig;
  for (const l of lights) l.visible = lit;
  scene.environment = mode === 'wire' ? null : envTex;
  const none = lit && !lights.length && !emissive;
  scene.environmentIntensity = lit ? (none ? 0.25 : 0.03) : mode === 'shaded' ? 0.45 : 0.6;
  key.visible = mode === 'shaded' || mode === 'textured';
  key.intensity = mode === 'shaded' ? 1.5 : 1.3;
  ground.visible = mode !== 'wire';
  ground.material.opacity = lit ? 0.6 : 0.35;
  scene.background = lit ? new THREE.Color(0x0c0b0a) : null;
  stage.dataset.mode = mode;
  if (mode !== 'textured') poster.hidden = true;
  note.hidden = !(root && none);
  document.querySelectorAll('#modes button').forEach(b => b.classList.toggle('on', b.dataset.mode === mode));
}

function setMode(m) {
  if (!MODES.includes(m)) return;
  mode = m; localStorage.viewMode = m; applyMode();
}

function showLoading(on, posterUrl) {
  // the thumbnail is a textured render, so only use it as a placeholder in textured mode
  if (on && posterUrl && mode === 'textured') { poster.src = posterUrl; poster.hidden = false; }
  else poster.hidden = true;
  stage.classList.toggle('loading', on);
}

function load(url, posterUrl) {
  const id = ++loadId;
  stopAnimation();
  if (root) { scene.remove(root); dispose(root); root = null; meshes = []; lights = []; }  // never show the previous model under the new placeholder
  note.hidden = true;
  showLoading(true, posterUrl);
  new GLTFLoader().load(url, gltf => {
    if (id !== loadId) return dispose(gltf.scene);
    stopAnimation();
    if (root) { scene.remove(root); dispose(root); }
    root = gltf.scene; meshes = []; lights = []; emissive = false;
    const seen = new Set();
    let shadowBudget = 16;
    root.traverse(o => {
      if (o.isMesh) {
        o.castShadow = o.receiveShadow = true;
        o.userData.orig = o.material;
        for (const m of [].concat(o.material))
          if (m.emissive && m.emissive.getHex() && (m.emissiveIntensity ?? 1) > 0) {
            emissive = true;
            if (FLICKER.test(m.name) && !seen.has(m)) {
              seen.add(m); flickerMats.push({m, base: m.emissiveIntensity, seed: Math.random() * 100});
            }
          }
        meshes.push(o);
      } else if (o.isLight) {
        o.intensity *= LIGHT_SCALE;
        // shadows keep lights from leaking through geometry (as they would in Blender); point-light
        // shadows cost 6 passes each, so cap them
        const cost = o.isPointLight ? 6 : 1;
        if (shadowBudget >= cost) {
          o.castShadow = true; shadowBudget -= cost;
          o.shadow.mapSize.set(o.isPointLight ? 512 : 1024, o.isPointLight ? 512 : 1024);
          o.shadow.bias = -0.001; o.shadow.normalBias = 0.02;
        }
        // the exporter names the light after Blender's light data and its parent node after the object
        if (FLICKER.test(o.name) || FLICKER.test(o.parent?.name || ''))
          flickerLights.push({l: o, base: o.intensity, seed: Math.random() * 100});  // base already in three.js units
        lights.push(o);
      }
    });
    scene.add(root);
    if (gltf.animations.length) {
      mixer = new THREE.AnimationMixer(root);
      for (const clip of gltf.animations) mixer.clipAction(clip).play();  // LoopRepeat by default
    }
    clock.getDelta();  // don't jump on the first frame
    syncAnimUi();
    frame(); applyMode();
    showLoading(false);
  }, undefined, err => { if (id === loadId) { console.error(err); showLoading(false); } });
}

function resize() {
  const w = stage.clientWidth, h = stage.clientHeight;
  if (!w || !h) return;
  renderer.setSize(w, h);
  composer.setSize(w, h);
  bloom.resolution.set(w, h);
  camera.aspect = w / h; camera.updateProjectionMatrix();
}
new ResizeObserver(resize).observe(stage);

function animated() { return !!(mixer || flickerLights.length || flickerMats.length); }
function syncAnimUi() {
  const b = document.getElementById('animBtn');
  if (!b) return;
  b.hidden = !animated();
  b.querySelector('span').textContent = paused ? 'Play animation' : 'Pause animation';
}
function setPaused(p) { paused = p; clock.getDelta(); syncAnimUi(); }

renderer.setAnimationLoop(() => {
  if (!root) { renderer.clear(); return; }
  const dt = Math.min(clock.getDelta(), 0.1);
  if (!paused && !reducedMotion.matches) {
    if (mixer) mixer.update(dt);
    const t = clock.elapsedTime;
    for (const f of flickerLights) f.l.intensity = f.base * flicker(t, f.seed);
    for (const f of flickerMats) f.m.emissiveIntensity = f.base * flicker(t, f.seed);
  }
  controls.update();
  if (mode === 'lights') composer.render(); else renderer.render(scene, camera);
});

document.getElementById('modes').addEventListener('click', e => {
  const b = e.target.closest('button[data-mode]');
  if (b) setMode(b.dataset.mode);
});
addEventListener('keydown', e => {
  if (e.target.closest('textarea,input')) return;
  const i = '1234'.indexOf(e.key);
  if (i >= 0) setMode(MODES[i]);
});

resize(); applyMode();
document.getElementById('animBtn')?.addEventListener('click', () => setPaused(!paused));
reducedMotion.addEventListener('change', syncAnimUi);
window.V = {load, clear, setMode, camera, setPaused, get animated() { return animated(); }, get mixer() { return mixer; }};
if (window.__pendingModel) { window.__pendingModel(); window.__pendingModel = null; }
