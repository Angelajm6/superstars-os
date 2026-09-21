/* #336 — the building in 3D (stage 1). Vendored three.js (MIT), no CDN.
 *
 * open(host, model, elevations, hooks) draws the job's building from its real footprint
 * and height (or the fallback box), the neighbours around it, and every traced elevation
 * as a drop × level grid draped on the matching wall. Cells are the same cells the 2D
 * markup shows; clicking one hands (elevationId, dropId, cellId) back to the page, which
 * opens its usual panel. Orbit with a drag, zoom with the wheel or a pinch, tap a face
 * label or the N/E/S/W buttons to fly there.
 *
 * hooks: { toneColor(tone) -> css color, statusOf(elevPayload, key) -> {label, tone},
 *          onPick(elevId, dropId, cellId), onHover(text|null) }
 */
import * as THREE from '/files/static/vendor/three.module.min.js';

const FT = 0.3048;
let R = null;   // the live renderer state, one at a time

function cssColor(c, fallback) {
  // resolve a CSS variable or colour into a hex number three.js can use
  try {
    const probe = document.createElement('span');
    probe.style.color = c; probe.style.display = 'none';
    document.body.appendChild(probe);
    const rgb = getComputedStyle(probe).color; probe.remove();
    const m = rgb.match(/(\d+),\s*(\d+),\s*(\d+)/);
    if (m) return (parseInt(m[1]) << 16) | (parseInt(m[2]) << 8) | parseInt(m[3]);
  } catch (e) {}
  return fallback;
}

function shapeFrom(ring) {
  const s = new THREE.Shape();
  ring.forEach(([x, y], i) => { if (i === 0) s.moveTo(x, y); else s.lineTo(x, y); });
  s.closePath();
  return s;
}

function extrude(ring, height, color, opacity) {
  const geo = new THREE.ExtrudeGeometry(shapeFrom(ring), { depth: height, bevelEnabled: false });
  const mat = new THREE.MeshLambertMaterial({ color, transparent: opacity < 1, opacity });
  const mesh = new THREE.Mesh(geo, mat);
  mesh.rotation.x = -Math.PI / 2;          // shape y (north) -> world -z, extrusion -> +y
  const edges = new THREE.LineSegments(new THREE.EdgesGeometry(geo, 20), new THREE.LineBasicMaterial({ color: 0x8c92a0, transparent: true, opacity: 0.6 }));
  edges.rotation.x = -Math.PI / 2;
  return [mesh, edges];
}

function boxRing(w, d) { return [[-w / 2, -d / 2], [w / 2, -d / 2], [w / 2, d / 2], [-w / 2, d / 2]]; }

function wallsFromBox(w, d) {
  // CCW ring: outward normals per edge, matching the server's convention
  return {
    S: { x0: -w / 2, y0: -d / 2, x1: w / 2, y1: -d / 2, length_m: w, normal: [0, -1] },
    E: { x0: w / 2, y0: -d / 2, x1: w / 2, y1: d / 2, length_m: d, normal: [1, 0] },
    N: { x0: w / 2, y0: d / 2, x1: -w / 2, y1: d / 2, length_m: w, normal: [0, 1] },
    W: { x0: -w / 2, y0: d / 2, x1: -w / 2, y1: -d / 2, length_m: d, normal: [-1, 0] },
  };
}

export function open(host, model, elevations, hooks) {
  close();
  host.innerHTML = '';
  const width = host.clientWidth || 800, height = Math.max(420, Math.round(width * 0.62));
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setSize(width, height);
  renderer.domElement.style.cssText = 'display:block;width:100%;height:auto;border-radius:12px;background:linear-gradient(#f7f8fb,#eef1f7);touch-action:none;cursor:grab';
  host.appendChild(renderer.domElement);

  const scene = new THREE.Scene();
  scene.add(new THREE.HemisphereLight(0xffffff, 0xd9dde6, 1.05));
  const sun = new THREE.DirectionalLight(0xffffff, 0.9); sun.position.set(60, 120, 40); scene.add(sun);

  const g = model.geometry, fb = model.fallback;
  const ring = (g && g.available && g.rings && g.rings.length) ? g.rings[0] : boxRing(fb.width_m, fb.depth_m);
  const H = (g && g.available) ? g.height_m : fb.height_m;
  const walls = (g && g.available) ? g.walls : wallsFromBox(fb.width_m, fb.depth_m);

  // ground + neighbours
  const radius = 120;
  const ground = new THREE.Mesh(new THREE.CircleGeometry(radius, 64), new THREE.MeshLambertMaterial({ color: 0xe9ecf2 }));
  ground.rotation.x = -Math.PI / 2; ground.position.y = -0.05; scene.add(ground);
  const grid = new THREE.GridHelper(radius * 2, 24, 0xd5d9e2, 0xe3e6ec); grid.position.y = -0.02; scene.add(grid);
  ((g && g.neighbours) || []).forEach(n => (n.rings || []).forEach(r => extrude(r, n.height_m || 8, 0xd8dce4, 0.95).forEach(m => scene.add(m))));

  // the building
  extrude(ring, H, 0xfdfcf9, 1).forEach(m => scene.add(m));

  // facade grids
  const pickables = [], labels = [];
  const toneHex = t => cssColor(hooks.toneColor(t), 0x9aa3ae);
  Object.entries(model.mapping || {}).forEach(([face, elevId]) => {
    const wall = walls[face], ep = elevations[elevId];
    if (!wall || !ep || !ep.geometry || !ep.geometry.facade) return;
    const fac = ep.geometry.facade, levels = ep.geometry.levels || [];
    const wf = fac.width_ft || 1, hf = fac.height_ft || 1;
    const sx = wall.length_m / (wf * FT), sy = H / (hf * FT);   // fit the traced facade onto the real wall
    const dir = [(wall.x1 - wall.x0) / wall.length_m, (wall.y1 - wall.y0) / wall.length_m];
    const nrm = wall.normal, off = 0.12;
    const lv = {}; levels.forEach(l => { lv[l.id] = l; });
    const byDrop = {}; (ep.drops || []).forEach(d => { byDrop[d.id] = d; });
    const yaw = Math.atan2(nrm[0], -nrm[1]);  // rotation about y so the plane faces outward (world z = -north)
    (ep.cells || []).forEach(c => {
      const d = byDrop[c.drop_id], L = lv[c.level_id]; if (!d || !L) return;
      const u0 = d.x0 * FT * sx, u1 = d.x1 * FT * sx, w = Math.max(0.2, u1 - u0 - 0.08);
      const z0 = L.bot * FT * sy, z1 = L.top * FT * sy, h = Math.max(0.2, z1 - z0 - 0.08);
      const st = hooks.statusOf(ep, c.status);
      const filled = c.status !== 'not_started';
      const mat = new THREE.MeshLambertMaterial({ color: filled ? toneHex(st.tone) : 0xffffff, transparent: !filled, opacity: filled ? 1 : 0.35, side: THREE.DoubleSide });
      const m = new THREE.Mesh(new THREE.PlaneGeometry(w, h), mat);
      const um = (u0 + u1) / 2, zm = (z0 + z1) / 2;
      const px = wall.x0 + dir[0] * um + nrm[0] * off, py = wall.y0 + dir[1] * um + nrm[1] * off;
      m.position.set(px, zm, -py); m.rotation.y = yaw;
      m.userData = { elevId: Number(elevId), dropId: d.id, cellId: c.id, text: 'DROP ' + d.idx + ' · ' + (c.level_name || '') + ' · ' + (st.label || c.status) + (c.reason ? ' — ' + c.reason : '') };
      scene.add(m); pickables.push(m);
      const line = new THREE.LineSegments(new THREE.EdgesGeometry(m.geometry), new THREE.LineBasicMaterial({ color: 0x14161c, transparent: true, opacity: 0.25 }));
      line.position.copy(m.position); line.rotation.copy(m.rotation); scene.add(line);
    });
    const mx = wall.x0 + dir[0] * wall.length_m / 2 + nrm[0] * 1.5, my = wall.y0 + dir[1] * wall.length_m / 2 + nrm[1] * 1.5;
    labels.push({ face, elevId: Number(elevId), pos: new THREE.Vector3(mx, H + 1.2, -my), text: ({ N: 'North', S: 'South', E: 'East', W: 'West' })[face] + (ep.name ? ' · ' + ep.name : '') });
  });

  // camera + controls
  const target = new THREE.Vector3(0, H / 2, 0);
  const size = Math.max(H, ...ring.map(p => Math.hypot(p[0], p[1]) * 2), 20);
  const cam = new THREE.PerspectiveCamera(45, width / height, 0.5, 2000);
  const view = { yaw: Math.PI * 0.75, pitch: 0.42, dist: size * 1.9 };
  function place() {
    cam.position.set(target.x + Math.sin(view.yaw) * Math.cos(view.pitch) * view.dist, target.y + Math.sin(view.pitch) * view.dist, target.z + Math.cos(view.yaw) * Math.cos(view.pitch) * view.dist);
    cam.lookAt(target);
  }
  place();

  // overlay: labels, tooltip, buttons
  host.style.position = 'relative';
  const overlay = document.createElement('div'); overlay.style.cssText = 'position:absolute;inset:0;pointer-events:none;'; host.appendChild(overlay);
  const labelEls = labels.map(l => { const e = document.createElement('button'); e.type = 'button'; e.textContent = l.text; e.style.cssText = 'position:absolute;transform:translate(-50%,-100%);pointer-events:auto;font:700 12px/1 Inter,system-ui,sans-serif;color:#14161C;background:#fff;border:1px solid #E3E5EA;border-radius:999px;padding:6px 10px;box-shadow:0 2px 8px rgba(0,0,0,.08);cursor:pointer;white-space:nowrap'; e.addEventListener('click', () => flyTo(l.face)); overlay.appendChild(e); return e; });
  const tip = document.createElement('div'); tip.style.cssText = 'position:absolute;display:none;background:#14161C;color:#fff;font:12.5px/1.35 Inter,system-ui,sans-serif;padding:8px 10px;border-radius:8px;max-width:260px;pointer-events:none;box-shadow:0 8px 24px rgba(20,22,28,.3)'; overlay.appendChild(tip);
  const bar = document.createElement('div'); bar.style.cssText = 'position:absolute;left:10px;top:10px;display:flex;gap:6px;pointer-events:auto;flex-wrap:wrap';
  const btn = (t, fn, title) => { const b = document.createElement('button'); b.type = 'button'; b.textContent = t; b.title = title || ''; b.style.cssText = 'font:700 12px Inter,system-ui,sans-serif;padding:6px 10px;border-radius:8px;border:1px solid #E3E5EA;background:#fff;color:#24418C;cursor:pointer'; b.addEventListener('click', fn); bar.appendChild(b); return b; };
  ['N', 'E', 'S', 'W'].forEach(f => { if (walls[f]) btn(f, () => flyTo(f), 'Face the ' + f + ' wall'); });
  btn('Reset', () => { view.yaw = Math.PI * 0.75; view.pitch = 0.42; view.dist = size * 1.9; place(); }, 'Reset the view');
  overlay.appendChild(bar);
  const hint = document.createElement('div'); hint.textContent = (g && g.available ? 'Real footprint and height from NYC Open Data' : 'Footprint not available — sized from the drawings') + ' · drag to orbit · scroll to zoom · tap a cell'; hint.style.cssText = 'position:absolute;left:10px;bottom:8px;font:11.5px Inter,system-ui,sans-serif;color:#8C92A0;pointer-events:none'; overlay.appendChild(hint);

  function flyTo(face) {
    const yawFor = { N: Math.PI, E: Math.PI / 2, S: 0, W: -Math.PI / 2 }[face];
    if (yawFor == null) return;
    const from = { ...view }, to = { yaw: yawFor, pitch: 0.18, dist: Math.max(size * 1.35, 30) }, t0 = performance.now();
    let dy = to.yaw - from.yaw; while (dy > Math.PI) dy -= 2 * Math.PI; while (dy < -Math.PI) dy += 2 * Math.PI;
    (function step() { const k = Math.min(1, (performance.now() - t0) / 520), e = 1 - Math.pow(1 - k, 3); view.yaw = from.yaw + dy * e; view.pitch = from.pitch + (to.pitch - from.pitch) * e; view.dist = from.dist + (to.dist - from.dist) * e; place(); if (k < 1 && R) requestAnimationFrame(step); })();
  }

  // pointer: orbit / zoom / pick
  const ray = new THREE.Raycaster(), ndc = new THREE.Vector2();
  let drag = null, hovered = null;
  const el = renderer.domElement;
  const toNdc = ev => { const r = el.getBoundingClientRect(); ndc.set(((ev.clientX - r.left) / r.width) * 2 - 1, -((ev.clientY - r.top) / r.height) * 2 + 1); return r; };
  const pick = ev => { toNdc(ev); ray.setFromCamera(ndc, cam); const hit = ray.intersectObjects(pickables, false)[0]; return hit ? hit.object : null; };
  const pinch = { d: 0 };
  el.addEventListener('pointerdown', ev => { if (ev.pointerType === 'touch' && ev.isPrimary === false) return; drag = { x: ev.clientX, y: ev.clientY, moved: 0 }; el.setPointerCapture(ev.pointerId); el.style.cursor = 'grabbing'; });
  el.addEventListener('pointermove', ev => {
    if (drag) { const dx = ev.clientX - drag.x, dy = ev.clientY - drag.y; drag.moved += Math.abs(dx) + Math.abs(dy); view.yaw -= dx * 0.008; view.pitch = Math.max(0.05, Math.min(1.35, view.pitch + dy * 0.006)); drag.x = ev.clientX; drag.y = ev.clientY; place(); return; }
    const o = pick(ev);
    if (o !== hovered) { if (hovered) hovered.material.emissive && hovered.material.emissive.setHex(0); hovered = o; if (o) { o.material.emissive.setHex(0x223355); } el.style.cursor = o ? 'pointer' : 'grab'; }
    if (o) { const r = el.getBoundingClientRect(); tip.textContent = o.userData.text; tip.style.display = 'block'; tip.style.left = Math.min(ev.clientX - r.left + 14, r.width - 270) + 'px'; tip.style.top = (ev.clientY - r.top + 14) + 'px'; hooks.onHover && hooks.onHover(o.userData.text); }
    else { tip.style.display = 'none'; hooks.onHover && hooks.onHover(null); }
  });
  const up = ev => { if (!drag) return; const moved = drag.moved; drag = null; el.style.cursor = 'grab'; if (moved < 6) { const o = pick(ev); if (o) hooks.onPick(o.userData.elevId, o.userData.dropId, o.userData.cellId); } };
  el.addEventListener('pointerup', up); el.addEventListener('pointercancel', () => { drag = null; el.style.cursor = 'grab'; });
  el.addEventListener('wheel', ev => { ev.preventDefault(); view.dist = Math.max(size * 0.5, Math.min(size * 6, view.dist * (1 + Math.sign(ev.deltaY) * 0.12))); place(); }, { passive: false });
  el.addEventListener('touchstart', ev => { if (ev.touches.length === 2) { pinch.d = Math.hypot(ev.touches[0].clientX - ev.touches[1].clientX, ev.touches[0].clientY - ev.touches[1].clientY); drag = null; } }, { passive: true });
  el.addEventListener('touchmove', ev => { if (ev.touches.length === 2 && pinch.d) { const d = Math.hypot(ev.touches[0].clientX - ev.touches[1].clientX, ev.touches[0].clientY - ev.touches[1].clientY); view.dist = Math.max(size * 0.5, Math.min(size * 6, view.dist * (pinch.d / d))); pinch.d = d; place(); } }, { passive: true });

  // resize + loop
  const onResize = () => { const w = host.clientWidth || width, h = Math.max(420, Math.round(w * 0.62)); renderer.setSize(w, h); cam.aspect = w / h; cam.updateProjectionMatrix(); };
  window.addEventListener('resize', onResize);
  const v = new THREE.Vector3();
  function frame() {
    if (!R) return;
    renderer.render(scene, cam);
    const r = el.getBoundingClientRect();
    labels.forEach((l, i) => { v.copy(l.pos).project(cam); const vis = v.z < 1; labelEls[i].style.display = vis ? '' : 'none'; if (vis) { labelEls[i].style.left = ((v.x + 1) / 2 * r.width) + 'px'; labelEls[i].style.top = ((1 - v.y) / 2 * r.height) + 'px'; } });
    requestAnimationFrame(frame);
  }
  R = { renderer, scene, onResize, pickables };
  frame();
  return { flyTo, cells: pickables.length, faces: labels.map(l => l.face), highlight(cellId) { pickables.forEach(m => { m.material.emissive.setHex(m.userData.cellId === cellId ? 0x2a4a99 : 0); }); } };
}

export function close() {
  if (!R) return;
  window.removeEventListener('resize', R.onResize);
  R.scene.traverse(o => { if (o.geometry) o.geometry.dispose(); if (o.material) { (Array.isArray(o.material) ? o.material : [o.material]).forEach(m => m.dispose()); } });
  R.renderer.dispose();
  R = null;
}
