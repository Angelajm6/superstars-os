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
 *          onPick(elevId, dropId, cellId), onHover(text|null),
 *          onMode(mode) (#353: 'photoreal' | 'massing'), onPhotorealError(message) }
 *
 * #353 — opts.photoreal = model.photoreal (available/key/lat/lng/logo_url/ground_ellipsoid_m).
 * When set, Google's Photorealistic 3D Tiles replace the massing, anchored at the model's
 * reference point (the same origin as every local coordinate here), and the drop x level
 * grid floats semi-transparent on the real walls, still clickable. The tiles library is
 * loaded only in this mode; the page's import map resolves its bare "three" imports to
 * the vendored build. Google's logo + data credits are shown along the bottom (policy).
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

const TILES = '/files/static/vendor/3d-tiles-renderer/';
const DRACO = '/files/static/vendor/three-addons/libs/draco/gltf/';

export function open(host, model, elevations, hooks, opts) {
  close();
  const pr = (opts && opts.photoreal && opts.photoreal.available) ? opts.photoreal : null;
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

  // the facade grid (and, in massing mode, the whole model) lives in one group so photoreal
  // mode can lift it onto the real ground height
  const world = new THREE.Group(); scene.add(world);
  if (!pr) {
    // ground + neighbours
    const radius = 120;
    const ground = new THREE.Mesh(new THREE.CircleGeometry(radius, 64), new THREE.MeshLambertMaterial({ color: 0xe9ecf2 }));
    ground.rotation.x = -Math.PI / 2; ground.position.y = -0.05; world.add(ground);
    const grid = new THREE.GridHelper(radius * 2, 24, 0xd5d9e2, 0xe3e6ec); grid.position.y = -0.02; world.add(grid);
    ((g && g.neighbours) || []).forEach(n => (n.rings || []).forEach(r => extrude(r, n.height_m || 8, 0xd8dce4, 0.95).forEach(m => world.add(m))));
    // the building
    extrude(ring, H, 0xfdfcf9, 1).forEach(m => world.add(m));
  }

  // facade grids
  const pickables = [], labels = [], faceGroups = [];
  const toneHex = t => cssColor(hooks.toneColor(t), 0x9aa3ae);
  // #354 — over the real building the DRAWING is the height truth (NYC's recorded height is
  // roof-to-grade of whatever footprint matched); massing keeps fitting the grid to NYC's H.
  let Hv = H;
  Object.entries(model.mapping || {}).forEach(([face, elevId]) => {
    const wall = walls[face], ep = elevations[elevId];
    if (!wall || !ep || !ep.geometry || !ep.geometry.facade) return;
    const fac = ep.geometry.facade, levels = ep.geometry.levels || [];
    const wf = fac.width_ft || 1, hf = fac.height_ft || 1;
    const sx = wall.length_m / (wf * FT), sy = pr ? 1 : H / (hf * FT);   // fit the traced facade onto the real wall
    if (pr) Hv = Math.max(Hv === H ? 0 : Hv, hf * FT);
    const fg = new THREE.Group(); world.add(fg);   // one group per wall: photoreal snaps it onto the real facade
    faceGroups.push({ fg, wall, nrm: wall.normal, hf: hf * FT });
    const dir = [(wall.x1 - wall.x0) / wall.length_m, (wall.y1 - wall.y0) / wall.length_m];
    const nrm = wall.normal, off = pr ? 0.8 : 0.12;
    const lv = {}; levels.forEach(l => { lv[l.id] = l; });
    const byDrop = {}; (ep.drops || []).forEach(d => { byDrop[d.id] = d; });
    const yaw = Math.atan2(nrm[0], -nrm[1]);  // rotation about y so the plane faces outward (world z = -north)
    (ep.cells || []).forEach(c => {
      const d = byDrop[c.drop_id], L = lv[c.level_id]; if (!d || !L) return;
      const u0 = d.x0 * FT * sx, u1 = d.x1 * FT * sx, w = Math.max(0.2, u1 - u0 - 0.08);
      const z0 = L.bot * FT * sy, z1 = L.top * FT * sy, h = Math.max(0.2, z1 - z0 - 0.08);
      const st = hooks.statusOf(ep, c.status);
      const filled = c.status !== 'not_started';
      // Lambert in both modes: hover/highlight drive .emissive. Over the photo the cells are
      // see-through and never hide the real facade behind them.
      const mat = pr
        ? new THREE.MeshLambertMaterial({ color: filled ? toneHex(st.tone) : 0xffffff, transparent: true, opacity: filled ? 0.55 : 0.12, side: THREE.DoubleSide, depthWrite: false })
        : new THREE.MeshLambertMaterial({ color: filled ? toneHex(st.tone) : 0xffffff, transparent: !filled, opacity: filled ? 1 : 0.35, side: THREE.DoubleSide });
      const m = new THREE.Mesh(new THREE.PlaneGeometry(w, h), mat);
      const um = (u0 + u1) / 2, zm = (z0 + z1) / 2;
      const px = wall.x0 + dir[0] * um + nrm[0] * off, py = wall.y0 + dir[1] * um + nrm[1] * off;
      m.position.set(px, zm, -py); m.rotation.y = yaw;
      m.userData = { elevId: Number(elevId), dropId: d.id, cellId: c.id, text: 'DROP ' + d.idx + ' · ' + (c.level_name || '') + ' · ' + (st.label || c.status) + (c.reason ? ' — ' + c.reason : '') };
      fg.add(m); pickables.push(m);
      const line = new THREE.LineSegments(new THREE.EdgesGeometry(m.geometry), new THREE.LineBasicMaterial({ color: pr ? 0xffffff : 0x14161c, transparent: true, opacity: pr ? 0.7 : 0.25 }));
      line.position.copy(m.position); line.rotation.copy(m.rotation); fg.add(line);
    });
    if (pr) {   // #354 — "DROP n" tags along the top of the wall so each column reads at a glance
      (ep.drops || []).forEach(d => {
        const um = ((d.x0 + d.x1) / 2) * FT * sx;
        labels.push({ small: true, fg, pos: new THREE.Vector3(wall.x0 + dir[0] * um + nrm[0] * off, hf * FT + 0.9, -(wall.y0 + dir[1] * um + nrm[1] * off)), text: 'DROP ' + d.idx });
      });
    }
    const mx = wall.x0 + dir[0] * wall.length_m / 2 + nrm[0] * 1.5, my = wall.y0 + dir[1] * wall.length_m / 2 + nrm[1] * 1.5;
    labels.push({ face, fg, elevId: Number(elevId), pos: new THREE.Vector3(mx, (pr ? hf * FT : H) + (pr ? 3.2 : 1.2), -my), text: ({ N: 'North', S: 'South', E: 'East', W: 'West' })[face] + (ep.name ? ' · ' + ep.name : '') });
  });

  // camera + controls
  const target = new THREE.Vector3(0, Hv / 2, 0);
  const size = Math.max(H, ...ring.map(p => Math.hypot(p[0], p[1]) * 2), 20);
  const cam = new THREE.PerspectiveCamera(45, width / height, 0.5, pr ? 20000 : 2000);
  const view = { yaw: Math.PI * 0.75, pitch: 0.42, dist: size * 1.9 };
  function place() {
    cam.position.set(target.x + Math.sin(view.yaw) * Math.cos(view.pitch) * view.dist, target.y + Math.sin(view.pitch) * view.dist, target.z + Math.cos(view.yaw) * Math.cos(view.pitch) * view.dist);
    cam.lookAt(target);
  }
  place();

  // overlay: labels, tooltip, buttons
  host.style.position = 'relative';
  const overlay = document.createElement('div'); overlay.style.cssText = 'position:absolute;inset:0;pointer-events:none;'; host.appendChild(overlay);
  const labelEls = labels.map(l => {
    const e = document.createElement(l.small ? 'span' : 'button'); e.textContent = l.text;
    if (l.small) {
      e.style.cssText = 'position:absolute;transform:translate(-50%,-100%);pointer-events:none;font:800 10.5px/1 Inter,system-ui,sans-serif;color:#fff;background:rgba(20,22,28,.72);border-radius:6px;padding:3px 5px;white-space:nowrap';
    } else {
      e.type = 'button';
      e.style.cssText = 'position:absolute;transform:translate(-50%,-100%);pointer-events:auto;font:700 12px/1 Inter,system-ui,sans-serif;color:#14161C;background:#fff;border:1px solid #E3E5EA;border-radius:999px;padding:6px 10px;box-shadow:0 2px 8px rgba(0,0,0,.08);cursor:pointer;white-space:nowrap';
      e.addEventListener('click', () => flyTo(l.face));
    }
    overlay.appendChild(e); return e;
  });
  const tip = document.createElement('div'); tip.style.cssText = 'position:absolute;display:none;background:#14161C;color:#fff;font:12.5px/1.35 Inter,system-ui,sans-serif;padding:8px 10px;border-radius:8px;max-width:260px;pointer-events:none;box-shadow:0 8px 24px rgba(20,22,28,.3)'; overlay.appendChild(tip);
  const bar = document.createElement('div'); bar.style.cssText = 'position:absolute;left:10px;top:10px;display:flex;gap:6px;pointer-events:auto;flex-wrap:wrap';
  const btn = (t, fn, title) => { const b = document.createElement('button'); b.type = 'button'; b.textContent = t; b.title = title || ''; b.style.cssText = 'font:700 12px Inter,system-ui,sans-serif;padding:6px 10px;border-radius:8px;border:1px solid #E3E5EA;background:#fff;color:#24418C;cursor:pointer'; b.addEventListener('click', fn); bar.appendChild(b); return b; };
  ['N', 'E', 'S', 'W'].forEach(f => { if (walls[f]) btn(f, () => flyTo(f), 'Face the ' + f + ' wall'); });
  btn('Reset', () => { view.yaw = Math.PI * 0.75; view.pitch = 0.42; view.dist = size * 1.9; place(); }, 'Reset the view');
  if (hooks.onMode && opts && opts.prAvailable) {   // #353 — shown in both modes once set up
    btn(pr ? 'Massing' : 'Photoreal', () => hooks.onMode(pr ? 'massing' : 'photoreal'), pr ? 'Show the simple massing model' : 'Show the real building (Google 3D)');
  }
  overlay.appendChild(bar);
  const hint = document.createElement('div'); hint.textContent = (pr ? 'Real building (Google 3D) with your drop grid' : (g && g.available ? 'Real footprint and height from NYC Open Data' : 'Footprint not available — sized from the drawings')) + ' · drag to orbit · scroll to zoom · tap a cell'; hint.style.cssText = 'position:absolute;left:10px;bottom:' + (pr ? '30px' : '8px') + ';font:11.5px Inter,system-ui,sans-serif;color:' + (pr ? '#fff' : '#8C92A0') + ';text-shadow:' + (pr ? '0 1px 2px rgba(0,0,0,.6)' : 'none') + ';pointer-events:none'; overlay.appendChild(hint);

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

  // #353 — Google Photorealistic 3D Tiles
  let tiles = null, credits = null, groundFound = false;
  if (pr) {
    world.position.y = pr.ground_ellipsoid_m || 0;   // first guess; refined by the ray below
    target.y = world.position.y + Hv / 2; place();
    renderer.domElement.style.background = '#cfd8e6';
    credits = document.createElement('div');
    credits.style.cssText = 'position:absolute;left:0;right:0;bottom:0;display:flex;align-items:center;gap:8px;padding:5px 10px;background:rgba(255,255,255,.85);font:11px Inter,system-ui,sans-serif;color:#3c414d;pointer-events:none;border-radius:0 0 12px 12px';
    const logo = document.createElement('img'); logo.src = pr.logo_url; logo.alt = 'Google Maps'; logo.style.cssText = 'height:18px;width:auto;margin:0 10px 0 0';   // Google: 16-19px tall, unmodified
    const txt = document.createElement('span'); credits.appendChild(logo); credits.appendChild(txt); overlay.appendChild(credits);
    const fail = msg => { if (hooks.onPhotorealError) hooks.onPhotorealError(msg); };
    if (pr.__testTiles) {   // test seam: a stand-in mesh with a known ground + facade (no network)
      tiles = pr.__testTiles; scene.add(tiles.group);
      setTimeout(() => { if (R) { R.tiles = tiles; fitToTiles(true); } }, 0);
    } else Promise.all([import(TILES + 'index.three.js'), import(TILES + 'index.three-plugins.js'), import(TILES + 'index.core-plugins.js'),
                 import('/files/static/vendor/three-addons/loaders/DRACOLoader.js')])
      .then(([core, plugins, corePlugins, draco]) => {
        if (!R) return;
        tiles = new core.TilesRenderer();
        tiles.registerPlugin(new corePlugins.GoogleCloudAuthPlugin({ apiToken: pr.key, autoRefreshToken: true, logoUrl: pr.logo_url }));
        tiles.registerPlugin(new plugins.GLTFExtensionsPlugin({ dracoLoader: new draco.DRACOLoader().setDecoderPath(DRACO) }));
        tiles.registerPlugin(new plugins.TileCompressionPlugin());
        // anchor the globe at the model's reference point; azimuth pi turns the library's
        // (+Z north, +X west) frame into this file's (+X east, -Z north)
        tiles.registerPlugin(new plugins.ReorientationPlugin({ lat: pr.lat * Math.PI / 180, lon: pr.lng * Math.PI / 180, height: 0, azimuth: Math.PI }));
        tiles.errorTarget = 6;
        tiles.setCamera(cam); tiles.setResolutionFromRenderer(cam, renderer);
        tiles.addEventListener('load-error', e => { if (!tiles.root && e && e.url && String(e.url).indexOf('root') !== -1) fail('Google 3D tiles could not load (key, quota or coverage).'); });
        tiles.addEventListener('load-model', () => { fitToTiles(); });
        scene.add(tiles.group);
        R.tiles = tiles;
      })
      .catch(() => fail('The Google 3D viewer could not load.'));
  }
  // #354 — fit the grid to the real mesh, again and again as sharper tiles stream in:
  //  * street level = the MEDIAN of downward rays just outside each wall (one probe landing
  //    on a roof or in a rail cut can't drag it), never locked on the first coarse hit;
  //  * each wall's grid slides along its normal onto the real facade (a sideways ray),
  //    kept a little in front so the photo shows through.
  const ray2 = new THREE.Raycaster(), p0 = new THREE.Vector3(), dDown = new THREE.Vector3(0, -1, 0), dIn = new THREE.Vector3();
  let lastFit = 0;
  function hitY(x, z) {
    p0.set(x, world.position.y + 400, z); ray2.set(p0, dDown);
    const h = ray2.intersectObject(tiles.group, true)[0];
    return h ? h.point.y : null;
  }
  function fitToTiles(force) {
    if (!tiles || (!force && performance.now() - lastFit < 400)) return;
    lastFit = performance.now();
    const ys = [];
    Object.values(walls).forEach(w => {
      const mx = (w.x0 + w.x1) / 2 + w.normal[0] * 5, my = (w.y0 + w.y1) / 2 + w.normal[1] * 5;
      const y = hitY(mx, -my); if (y != null && Math.abs(y - (pr.ground_ellipsoid_m || 0)) < 120) ys.push(y);
    });
    if (ys.length) {
      ys.sort((a, b) => a - b);
      const g = ys[Math.floor((ys.length - 1) / 2)];
      if (Math.abs(g - world.position.y) > 0.05) { world.position.y = g; target.y = g + Hv / 2; place(); }
      groundFound = true;
    }
    faceGroups.forEach(f => {
      const w = f.wall, n = f.nrm, back = 25;
      const mx = (w.x0 + w.x1) / 2 + n[0] * back, my = (w.y0 + w.y1) / 2 + n[1] * back;
      p0.set(mx, world.position.y + Math.min(f.hf / 2, 12), -my); dIn.set(-n[0], 0, n[1]);
      ray2.set(p0, dIn);
      const h = ray2.intersectObject(tiles.group, true)[0];
      if (h && h.distance > back - 12 && h.distance < back + 12) {
        const shift = back - h.distance;      // + = the real facade stands proud of the footprint line
        f.fg.position.set(n[0] * shift, 0, -n[1] * shift);
      }
    });
  }
  let creditTick = 0;

  // resize + loop
  const onResize = () => { const w = host.clientWidth || width, h = Math.max(420, Math.round(w * 0.62)); renderer.setSize(w, h); cam.aspect = w / h; cam.updateProjectionMatrix(); };
  window.addEventListener('resize', onResize);
  const v = new THREE.Vector3();
  function frame() {
    if (!R) return;
    if (tiles) {
      cam.updateMatrixWorld(); tiles.update();
      if (performance.now() - lastFit > 1500) fitToTiles();   // keep converging as detail arrives
      if (credits && performance.now() - creditTick > 1000) {   // Google data credits, most frequent first
        creditTick = performance.now();
        const a = (tiles.getAttributions ? tiles.getAttributions() : []).filter(x => x.type === 'string').map(x => x.value);
        credits.lastChild.textContent = a.join(' · ');
      }
    }
    renderer.render(scene, cam);
    const r = el.getBoundingClientRect();
    labels.forEach((l, i) => { v.copy(l.pos); if (l.fg) { v.x += l.fg.position.x; v.z += l.fg.position.z; } v.y += world.position.y; v.project(cam); const vis = v.z < 1; labelEls[i].style.display = vis ? '' : 'none'; if (vis) { labelEls[i].style.left = ((v.x + 1) / 2 * r.width) + 'px'; labelEls[i].style.top = ((1 - v.y) / 2 * r.height) + 'px'; } });
    requestAnimationFrame(frame);
  }
  R = { renderer, scene, onResize, pickables, tiles: null, cam, world, faceGroups, fit: () => fitToTiles(true) };
  frame();
  return { flyTo, cells: pickables.length, faces: labels.map(l => l.face), highlight(cellId) { pickables.forEach(m => { m.material.emissive.setHex(m.userData.cellId === cellId ? 0x2a4a99 : 0); }); } };
}

export function close() {
  if (!R) return;
  window.removeEventListener('resize', R.onResize);
  if (R.tiles) { try { R.scene.remove(R.tiles.group); R.tiles.dispose(); } catch (e) {} }   // #353
  R.scene.traverse(o => { if (o.geometry) o.geometry.dispose(); if (o.material) { (Array.isArray(o.material) ? o.material : [o.material]).forEach(m => m.dispose()); } });
  R.renderer.dispose();
  R = null;
}

// read-only hook for debugging in the browser console (renderer state of the open view)
export function debugState() { return R; }
