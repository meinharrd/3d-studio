// First-person walk mode. Models opt in by naming walkable nodes walk_* (floors, stairs, paths)
// and optionally adding an empty named fpv_spawn at foot level, facing along the empty's +Y in
// Blender (= glTF -Z). Everything else is solid.
// Collision uses three-mesh-bvh with double-sided tests, so it behaves the same in every display
// mode (wire/clay materials are single-sided and would otherwise let you through walls from inside).
import * as THREE from 'three';
import {MeshBVH} from 'three-mesh-bvh';

const EYE = 1.6, STEP = 0.45, RADIUS = 0.3, WALK = 1.4, RUN = 5.0, UP = 0.7, GRAVITY = 9.81;
// wall rays: shin (low furniture like sofa seats), knee, chest
const RAYS = [0.15, 0.4, 1.2];
const LOOK = 0.0022, PITCH_MAX = THREE.MathUtils.degToRad(85), FOV = 70;
const WALK_RE = /^walk_/i;

let cam, controls, dom, stage, hintEl, btnEl, exitEl, joyEl;
let root = null, hasWalk = false, spawn = null, animated = new Set(), prepared = false;
let solids = [];          // {o, bvh, inv, nmat, sphere, walk}
let active = false, saved = null, yaw = 0, pitch = 0, locked = false, useLock = true;
let collide = false, vy = 0, falling = false;  // walls are passable unless toggled on (C)
const feet = new THREE.Vector3();
const keys = new Set();
const joy = {id: null, x0: 0, y0: 0, x: 0, y: 0};
const look = {id: null, x: 0, y: 0};
const _ray = new THREE.Ray(), _v = new THREE.Vector3(), _v2 = new THREE.Vector3();

export function init(ctx) {
  ({camera: cam, controls, dom, stage} = ctx);
  hintEl = document.getElementById('walkHint');
  btnEl = document.getElementById('walkBtn');
  exitEl = document.getElementById('walkExit');
  joyEl = document.getElementById('walkJoy');
  btnEl?.addEventListener('click', () => (active ? exit() : enterFromSpawn()));
  exitEl?.addEventListener('click', () => exit());

  // click (no drag) on a walkable surface in orbit view -> walk from there
  let down = null;
  dom.addEventListener('pointerdown', e => {
    if (active) return onWalkPointerDown(e);
    if (e.button === 0) down = {x: e.clientX, y: e.clientY, t: performance.now()};
  });
  dom.addEventListener('pointerup', e => {
    if (active) return onWalkPointerUp(e);
    if (!down || !hasWalk) return;
    const moved = Math.hypot(e.clientX - down.x, e.clientY - down.y), dt = performance.now() - down.t;
    down = null;
    if (moved > 5 || dt > 600) return;
    const hit = pick(e.clientX, e.clientY);
    if (hit && hit.e.walk && hit.n.y > UP) enter(hit.p, e.pointerType !== 'touch');
  });
  dom.addEventListener('pointermove', e => { if (active) onWalkPointerMove(e); });
  dom.addEventListener('pointercancel', e => { if (active) onWalkPointerUp(e); });

  document.addEventListener('pointerlockchange', () => {
    const now = document.pointerLockElement === dom;
    if (locked && !now && active) exit();   // Esc (or focus loss) releases the lock -> leave walk mode
    locked = now;
  });
  document.addEventListener('pointerlockerror', () => { useLock = false; });
  document.addEventListener('mousemove', e => {
    if (!active || !locked) return;
    turn(e.movementX, e.movementY);
  });
  addEventListener('keydown', e => {
    if (e.target.closest?.('textarea,input')) return;
    if (!active) return;
    if (e.key === 'Escape') { exit(); return; }
    if (e.code === 'KeyC' && !e.repeat) { collide = !collide; ui(); return; }
    keys.add(e.code);
    if (/^(Arrow|Space)/.test(e.code)) e.preventDefault();
  });
  addEventListener('keyup', e => keys.delete(e.code));
  addEventListener('blur', () => keys.clear());
}

function isWalk(o) {
  for (let p = o; p && p !== root.parent; p = p.parent) if (WALK_RE.test(p.name)) return true;
  return false;
}
function isAnimated(o) {
  for (let p = o; p && p !== root.parent; p = p.parent) if (animated.has(p.name)) return true;
  return false;
}
function visibleChain(o) {
  for (let p = o; p; p = p.parent) if (!p.visible) return false;
  return true;
}

export function setModel(r, animations = []) {
  exit();
  root = r; hasWalk = false; spawn = null; prepared = false; solids = [];
  animated = new Set(animations.flatMap(c => c.tracks.map(t => THREE.PropertyBinding.parseTrackName(t.name).nodeName)));
  if (r) r.traverse(o => {
    if (o.name === 'fpv_spawn') spawn = o;
    if (o.isMesh && WALK_RE.test(o.name)) hasWalk = true;
    else if (o.isMesh && isWalk(o)) hasWalk = true;
  });
  ui();
}

// Build collision data once per model (lazily, on first use).
function prepare() {
  if (prepared || !root) return;
  prepared = true;
  root.updateMatrixWorld(true);
  root.traverse(o => {
    if (!o.isMesh || o.isSkinnedMesh || !visibleChain(o) || isAnimated(o)) return;
    const g = o.geometry;
    // indirect: don't reorder the geometry's index in place — glTF geometries share buffers, and an
    // in-place build here produced BVHs that missed faces (e.g. the house's sofa seats)
    if (!g.userData.walkBVH) g.userData.walkBVH = new MeshBVH(g, {indirect: true});
    if (!g.boundingSphere) g.computeBoundingSphere();
    solids.push({
      o, bvh: g.userData.walkBVH, walk: isWalk(o),
      inv: o.matrixWorld.clone().invert(),
      nmat: new THREE.Matrix3().getNormalMatrix(o.matrixWorld),
      sphere: g.boundingSphere.clone().applyMatrix4(o.matrixWorld),
    });
  });
}

// Closest hit along a world-space ray (origin, unit dir, far) that passes filter(entry, normal, point).
// Normals are world-space and flipped to face the ray (surfaces are treated as double-sided).
function cast(origin, dir, far, filter) {
  let best = null;
  _ray.set(origin, dir);
  for (const e of solids) {
    const s = e.sphere;
    if (origin.distanceTo(s.center) - s.radius > far || !_ray.intersectsSphere(s)) continue;
    const lo = origin.clone().applyMatrix4(e.inv);
    const lf = _v.copy(origin).addScaledVector(dir, far).applyMatrix4(e.inv);
    const ldir = lf.clone().sub(lo);
    const lfar = ldir.length();
    if (lfar < 1e-9) continue;
    ldir.divideScalar(lfar);
    const hits = e.bvh.raycast(new THREE.Ray(lo, ldir), THREE.DoubleSide, 0, lfar);
    for (const h of hits) {
      const p = h.point.clone().applyMatrix4(e.o.matrixWorld);
      const d = p.distanceTo(origin);
      if (d > far || (best && d >= best.d)) continue;
      const n = h.face.normal.clone().applyMatrix3(e.nmat).normalize();
      if (n.dot(dir) > 0) n.negate();
      if (filter && !filter(e, n, p)) continue;
      best = {e, n, p, d};
    }
  }
  return best;
}

// glass and other see-through surfaces don't stop a click (checks the model's own material, so it
// works in wire/shaded mode too)
function seeThrough(o) {
  return [].concat(o.userData.orig || o.material).some(m => m && (m.transmission > 0 || (m.transparent && m.opacity < 0.9)
    || /glass/i.test(m.name || '')));
}

function pick(cx, cy) {
  prepare();
  const r = dom.getBoundingClientRect();
  const ndc = new THREE.Vector2(((cx - r.left) / r.width) * 2 - 1, -((cy - r.top) / r.height) * 2 + 1);
  const rc = new THREE.Raycaster();
  rc.setFromCamera(ndc, cam);
  return cast(rc.ray.origin, rc.ray.direction, cam.far, (e) => !seeThrough(e.o));  // first opaque hit
}

// walkable ground under a point: the highest upward walk_ face at most STEP above `from`
// (depth = how far down to look; falls can be long)
function ground(pos, from = pos.y, depth = 200) {
  const o = _v2.set(pos.x, from + STEP, pos.z);
  return cast(o.clone(), new THREE.Vector3(0, -1, 0), STEP + depth, (e, n) => e.walk && n.y > UP);
}

// wall hit along a horizontal move (rays at RAYS heights above the feet)
function wall(d) {
  const len = d.length();
  if (len < 1e-9) return null;
  const dir = d.clone().divideScalar(len);
  let best = null;
  for (const h of RAYS) {
    const hit = cast(new THREE.Vector3(feet.x, feet.y + h, feet.z), dir, len + RADIUS, (e, n, p) => {
      if (n.y > UP) return false;                          // a floor/top face, not a wall
      if (e.walk && p.y < feet.y + STEP) return false;     // stair risers and kerbs you can step onto
      return true;
    });
    if (hit && (!best || hit.d < best.d)) best = hit;
  }
  return best;
}

function tryMove(d) {
  for (let i = 0; collide && i < 3 && d.lengthSq() > 1e-12; i++) {
    const hit = wall(d);
    if (!hit) break;
    const n = _v.set(hit.n.x, 0, hit.n.z);
    if (n.lengthSq() < 1e-6) { d.set(0, 0, 0); break; }
    n.normalize();
    const into = d.dot(n);
    if (into < 0) d.addScaledVector(n, -into);           // slide along the wall
    else if (hit.d < RADIUS) { d.set(0, 0, 0); break; }
    else break;
  }
  if (d.lengthSq() < 1e-12) return false;
  const np = feet.clone().add(d);
  if (falling) { feet.x = np.x; feet.z = np.z; return true; }   // steer while falling
  const g = ground(np, feet.y);
  if (!g) return false;                                   // nothing walkable below at all: the void
  feet.set(np.x, feet.y, np.z);
  if (g.p.y >= feet.y - STEP) feet.y = g.p.y;             // stairs / small drops: snap
  else { falling = true; vy = 0; }                        // over a cliff: fall
  return true;
}

function fall(dt) {
  if (!falling) return;
  vy -= GRAVITY * dt;
  const dy = vy * dt;                                     // negative
  const g = ground(feet, feet.y - STEP, -dy + 0.02);     // anything to land on within this frame's drop?
  if (g && g.p.y >= feet.y + dy - 0.01) { feet.y = g.p.y; falling = false; vy = 0; }
  else if (feet.y + dy < -500) { falling = false; vy = 0; } // safety net
  else feet.y += dy;
}

function turn(dx, dy) {
  yaw -= dx * LOOK;
  pitch = THREE.MathUtils.clamp(pitch - dy * LOOK, -PITCH_MAX, PITCH_MAX);
}

function place() {
  cam.position.set(feet.x, feet.y + EYE, feet.z);
  cam.quaternion.setFromEuler(new THREE.Euler(pitch, yaw, 0, 'YXZ'));
}

export function update(dt) {
  if (!active) return false;
  const k = c => keys.has(c) ? 1 : 0;
  let f = k('KeyW') + k('ArrowUp') - k('KeyS') - k('ArrowDown') - joy.y;
  let s = k('KeyD') + k('ArrowRight') - k('KeyA') - k('ArrowLeft') + joy.x;
  const mag = Math.hypot(f, s);
  if (mag > 1) { f /= mag; s /= mag; }
  if (mag > 0.01) {
    const slow = keys.has('ShiftLeft') || keys.has('ShiftRight') || (joy.id !== null && Math.hypot(joy.x, joy.y) < 0.5);
    const speed = slow ? WALK : RUN;
    const fwd = new THREE.Vector3(-Math.sin(yaw), 0, -Math.cos(yaw));
    const right = new THREE.Vector3(Math.cos(yaw), 0, -Math.sin(yaw));
    const d = fwd.multiplyScalar(f).addScaledVector(right, s).multiplyScalar(speed * dt);
    // sub-step so fast moves can't tunnel through thin walls
    const steps = Math.max(1, Math.ceil(d.length() / 0.1));
    d.divideScalar(steps);
    for (let i = 0; i < steps; i++) tryMove(d.clone());
  }
  for (let t = Math.min(dt, 0.1); t > 1e-6; t -= 0.02) fall(Math.min(t, 0.02));  // sub-steps: correct at any fps
  place();
  return true;
}

function enter(point, lock = true) {
  if (!root) return;
  prepare();
  // flush OrbitControls' damping momentum first, so it can't nudge the camera when we come back
  const damp = controls.enableDamping;
  controls.enableDamping = false; controls.update(); controls.enableDamping = damp;
  saved = {pos: cam.position.clone(), target: controls.target.clone(), near: cam.near, far: cam.far,
           fov: cam.fov, autoRotate: controls.autoRotate};
  const dir = cam.getWorldDirection(new THREE.Vector3());
  yaw = Math.atan2(-dir.x, -dir.z); pitch = 0;
  feet.copy(point); falling = false; vy = 0;
  const g = ground(point, point.y, STEP);
  if (g) feet.y = g.p.y;
  controls.enabled = false; controls.autoRotate = false;
  cam.fov = FOV; cam.near = 0.05; cam.far = Math.max(cam.far, 500); cam.updateProjectionMatrix();
  active = true;
  keys.clear();
  if (lock && useLock && dom.requestPointerLock && matchMedia('(pointer: fine)').matches) {
    try { const p = dom.requestPointerLock(); if (p?.catch) p.catch(() => { useLock = false; }); } catch { useLock = false; }
  }
  place(); ui();
}

function enterFromSpawn() {
  if (!spawn) return;
  root.updateMatrixWorld(true);
  const p = spawn.getWorldPosition(new THREE.Vector3());
  enter(p);
  // face along the spawn's forward axis: glTF -Z, i.e. +Y of the Blender empty
  const f = new THREE.Vector3(0, 0, -1).applyQuaternion(spawn.getWorldQuaternion(new THREE.Quaternion()));
  yaw = Math.atan2(-f.x, -f.z);
  place();
}

export function exit() {
  if (!active) return;
  active = false;
  keys.clear(); joy.id = look.id = null; joy.x = joy.y = 0;
  if (document.pointerLockElement === dom) document.exitPointerLock();
  if (saved) {
    cam.position.copy(saved.pos); cam.fov = saved.fov; cam.near = saved.near; cam.far = saved.far;
    cam.updateProjectionMatrix();
    controls.target.copy(saved.target); controls.autoRotate = false;
  }
  controls.enabled = true; controls.update();
  ui();
}

export function isActive() { return active; }
// test/debug hook: state, and aim the walker (degrees; 0 = looking along -Z)
export function debug() { return {falling, collide, saved: saved && saved.pos.toArray().map(v => +v.toFixed(3)), active, feet: feet.toArray().map(v => +v.toFixed(3)), yaw: THREE.MathUtils.radToDeg(yaw), hasWalk, spawn: !!spawn, solids: solids.length}; }
export function aim(deg, pitchDeg = 0) { yaw = THREE.MathUtils.degToRad(deg); pitch = THREE.MathUtils.degToRad(pitchDeg); if (active) place(); }
export function teleport(x, y, z) { feet.set(x, y, z); falling = false; vy = 0; const g = ground(feet, y, STEP); if (g) feet.y = g.p.y; if (active) place(); return !!g; }
export function setCollide(on) { collide = on; ui(); }
export function probe(x, y, z, dx, dy, dz, far = 5) {
  prepare();
  const d = new THREE.Vector3(dx, dy, dz).normalize(), o = new THREE.Vector3(x, y, z), out = [];
  for (let i = 0; i < 6; i++) {
    const h = cast(o, d, far, (e, n, p) => !out.some(q => q.o === e.o && Math.abs(q.d - p.distanceTo(o)) < 1e-4));
    if (!h) break;
    out.push({o: h.e.o, d: h.d, name: h.e.o.name, walk: h.e.walk, n: h.n.toArray().map(v => +v.toFixed(2)), p: h.p.toArray().map(v => +v.toFixed(2))});
    o.copy(h.p).addScaledVector(d, 1e-3);
  }
  return out.map(({o, ...r}) => r);
}
export function pickAt(cx, cy) { const h = pick(cx, cy); return h && {name: h.e.o.name, walk: h.e.walk, ny: +h.n.y.toFixed(2), p: h.p.toArray().map(v => +v.toFixed(2))}; }

// ---- touch / no-pointer-lock: left half = joystick, right half = drag to look ----
function onWalkPointerDown(e) {
  if (locked) return;
  const r = dom.getBoundingClientRect();
  dom.setPointerCapture?.(e.pointerId);
  if (e.pointerType === 'touch' && e.clientX - r.left < r.width / 2 && joy.id === null) {
    Object.assign(joy, {id: e.pointerId, x0: e.clientX, y0: e.clientY, x: 0, y: 0});
    if (joyEl) { joyEl.hidden = false; joyEl.style.left = (e.clientX - r.left) + 'px'; joyEl.style.top = (e.clientY - r.top) + 'px'; }
  } else if (look.id === null) {
    Object.assign(look, {id: e.pointerId, x: e.clientX, y: e.clientY});
  }
}
function onWalkPointerMove(e) {
  if (e.pointerId === joy.id) {
    const R = 50, dx = e.clientX - joy.x0, dy = e.clientY - joy.y0, m = Math.hypot(dx, dy);
    const k = m > R ? R / m : 1;
    joy.x = dx * k / R; joy.y = dy * k / R;
    const knob = joyEl?.firstElementChild;
    if (knob) knob.style.transform = `translate(${dx * k}px, ${dy * k}px)`;
  } else if (e.pointerId === look.id && !locked) {
    turn((e.clientX - look.x) * 1.6, (e.clientY - look.y) * 1.6);
    look.x = e.clientX; look.y = e.clientY;
  }
}
function onWalkPointerUp(e) {
  if (e.pointerId === joy.id) {
    joy.id = null; joy.x = joy.y = 0;
    if (joyEl) { joyEl.hidden = true; joyEl.firstElementChild.style.transform = ''; }
  }
  if (e.pointerId === look.id) look.id = null;
}

function ui() {
  if (btnEl) { btnEl.hidden = !(hasWalk && spawn); btnEl.classList.toggle('on', active); }
  if (exitEl) exitEl.hidden = !active;
  if (hintEl) {
    const touch = matchMedia('(pointer: coarse)').matches;
    hintEl.hidden = !hasWalk;
    hintEl.textContent = !active ? (touch ? 'Tap a floor to walk' : 'Click a floor to walk')
      : touch ? 'Left: move (push far to run) · Right: look'
      : `WASD / arrows to move · Shift to walk slowly · C: walls ${collide ? 'solid' : 'passable'} · Esc to exit`;
    hintEl.classList.toggle('walking', active);
  }
  stage?.classList.toggle('walking', active);
}
