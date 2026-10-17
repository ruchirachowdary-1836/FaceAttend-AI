"use strict";
/* FaceAttend AI front-end: live recognition overlay, effects, analytics, enrollment, sessions. */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* storage blocked */ } },
};

const S = {
  view: "live", stream: null, ws: null, running: false, demo: false, inflight: false, t0: 0,
  tracks: [], cfg: {}, stats: null, students: [], feedCount: 0, token: store.get("token", ""),
  prefs: Object.assign({ voice: false, sound: false, calm: false, confetti: true, camera: "Entrance", theme: "dark" }, store.get("prefs", {})),
  frames: 0, fpsLast: performance.now(), session: null, welcomeQ: [], welcomeBusy: false, particles: [],
  enrollFor: null, enrollStream: null, charts: {}, demoTimer: null,
};
const video = $("#video"), overlay = $("#overlay"), fx = $("#fx"), stage = $("#stage");
const ctx = overlay.getContext("2d"), fctx = fx.getContext("2d");
const grab = document.createElement("canvas");
const savePrefs = () => store.set("prefs", S.prefs);

/* ---------- helpers ---------- */
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const initials = (n) => String(n).split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0]).join("").toUpperCase() || "?";
const hue = (s) => { let h = 0; for (const ch of String(s)) h = (h * 31 + ch.charCodeAt(0)) % 360; return h; };
const avatar = (name, cls = "") => `<div class="av ${cls}" style="--h:${hue(name)}">${esc(initials(name))}</div>`;
const fmtTime = (iso) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();

function toast(msg, kind = "info") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`; el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => el.classList.add("out"), 3600);
  setTimeout(() => el.remove(), 4100);
}

function countUp(el, to, suffix = "") {
  const from = parseInt(el.textContent) || 0, t0 = performance.now();
  if (from === to) { el.textContent = to + suffix; return; }
  const step = (now) => {
    const p = Math.min(1, (now - t0) / 800), e = 1 - Math.pow(1 - p, 3);
    el.textContent = Math.round(from + (to - from) * e) + suffix;
    if (p < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (S.token) headers.Authorization = "Bearer " + S.token;
  const init = { ...opts, headers };
  if (opts.json !== undefined) { headers["Content-Type"] = "application/json"; init.body = JSON.stringify(opts.json); }
  const r = await fetch(path, init);
  if (r.status === 401) { openLogin(); throw new Error("Admin login required"); }
  if (!r.ok) {
    let m = r.statusText;
    try { const j = await r.json(); m = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch { /* not json */ }
    throw new Error(m);
  }
  return (r.headers.get("content-type") || "").includes("json") ? r.json() : r;
}

/* ---------- theme, calm mode ---------- */
function applyPrefs() {
  document.documentElement.dataset.theme = S.prefs.theme;
  document.documentElement.classList.toggle("calm", S.prefs.calm);
  $("#voiceBtn").textContent = "Voice: " + (S.prefs.voice ? "on" : "off");
}
$("#themeBtn").onclick = () => { S.prefs.theme = S.prefs.theme === "dark" ? "light" : "dark"; savePrefs(); applyPrefs(); refreshCharts(); };
$("#voiceBtn").onclick = () => { S.prefs.voice = !S.prefs.voice; savePrefs(); applyPrefs(); if (S.prefs.voice) speak("Voice greetings on"); };

/* ---------- ambient particle network ---------- */
const bg = $("#bg"), bctx = bg.getContext("2d");
const dots = Array.from({ length: 56 }, () => ({ x: Math.random(), y: Math.random(), vx: (Math.random() - .5) * 2e-4, vy: (Math.random() - .5) * 2e-4 }));
function drawBg() {
  requestAnimationFrame(drawBg);
  if (S.prefs.calm || document.hidden) return;
  const W = bg.width = innerWidth, H = bg.height = innerHeight;
  bctx.clearRect(0, 0, W, H);
  const dark = S.prefs.theme === "dark";
  for (const d of dots) { d.x = (d.x + d.vx + 1) % 1; d.y = (d.y + d.vy + 1) % 1; }
  for (let i = 0; i < dots.length; i++) {
    const a = dots[i];
    for (let j = i + 1; j < dots.length; j++) {
      const b = dots[j], dx = (a.x - b.x) * W, dy = (a.y - b.y) * H, d = Math.hypot(dx, dy);
      if (d < 150) { bctx.strokeStyle = `rgba(${dark ? "140,150,255" : "90,100,200"},${(1 - d / 150) * .22})`; bctx.beginPath(); bctx.moveTo(a.x * W, a.y * H); bctx.lineTo(b.x * W, b.y * H); bctx.stroke(); }
    }
    bctx.fillStyle = dark ? "rgba(170,180,255,.55)" : "rgba(80,90,200,.45)";
    bctx.beginPath(); bctx.arc(a.x * W, a.y * H, 1.6, 0, 7); bctx.fill();
  }
}

/* ---------- clock ---------- */
function tickClock() {
  const d = new Date();
  $("#clockTime").textContent = d.toLocaleTimeString([], { hour12: false });
  $("#clockDate").textContent = d.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short", year: "numeric" });
  if (S.session && S.view === "sessions") updateTimer();
}
setInterval(tickClock, 1000);

/* ---------- navigation ---------- */
const TITLES = {
  live: ["Live <span>attendance</span>", "Real-time face recognition, marked in under a second"],
  analytics: ["Attendance <span>insights</span>", "Trends, heatmap and students who need attention"],
  students: ["<span>People</span> and enrollment", "Enroll, review and manage students"],
  sessions: ["Class <span>sessions</span>", "Start a class, track late arrivals, review history"],
  settings: ["Control <span>centre</span>", "Tune recognition, experience and tools"],
};
function show(view) {
  S.view = view;
  $$(".nav-btn[data-view]").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === "view-" + view));
  $("#title").innerHTML = TITLES[view][0]; $("#subtitle").textContent = TITLES[view][1];
  if (view === "analytics") loadAnalytics();
  if (view === "students") loadStudents();
  if (view === "sessions") loadSessions();
  if (view === "settings") loadSettings();
  if (view === "live") { loadStats(); resizeOverlay(); }
}
$$(".nav-btn[data-view]").forEach((b) => b.onclick = () => show(b.dataset.view));

/* ---------- WebSocket ---------- */
function setConn(online, text) {
  $("#connDot").classList.toggle("online", online); $("#connDot").title = text;
  $("#screensChip").classList.toggle("live", online);
}
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/live?camera=${encodeURIComponent(S.prefs.camera)}`);
  S.ws = ws;
  ws.onopen = () => { setConn(true, "Live"); S.inflight = false; };
  ws.onclose = () => { setConn(false, "Reconnecting"); S.inflight = false; setTimeout(connect, 1500); };
  ws.onerror = () => ws.close();
  ws.onmessage = (e) => handleMessage(JSON.parse(e.data));
}
function setSession(s) {
  S.session = s;
  $("#sessionName").textContent = s ? s.name : "none (paused)";
  $("#sessionChip").classList.toggle("live", !!s);
}
function handleMessage(m) {
  switch (m.type) {
    case "hello": case "config": S.cfg = m; setSession(m.session); updateAuthBtn(); if (S.view === "settings") loadSettings(); break;
    case "frame":
      S.inflight = false; S.frames++;
      $("#hudLat").textContent = Math.round(performance.now() - S.t0) + " ms";
      updateTracks(m); $("#hudFaces").textContent = `Faces ${m.faces.length}`; break;
    case "marked": onMarked(m); break;
    case "unmarked": loadStats(); loadStudents(true); break;
    case "unknown": addFeed({ unknown: true, time: m.time, camera: m.camera }); break;
    case "roster": loadStats(); loadStudents(true); if (m.reason === "enrolled") toast(`${m.name} enrolled`, "success"); break;
    case "session": setSession(m.session); loadStats(); if (S.view === "sessions") loadSessions(); break;
    case "presence": $("#screens").textContent = m.screens; $("#screensChip").title = m.cameras.length ? "Cameras: " + m.cameras.join(", ") : "Connected screens"; break;
  }
}

/* ---------- camera ---------- */
async function startCamera() {
  if (S.demo) stopDemo();
  try {
    S.stream = await navigator.mediaDevices.getUserMedia({ video: { width: { ideal: 1280 }, height: { ideal: 720 }, facingMode: "user" }, audio: false });
  } catch (err) { toast("Camera unavailable: " + err.message, "error"); return false; }
  video.srcObject = S.stream;
  await video.play().catch(() => {});
  if (!video.videoWidth) await new Promise((r) => video.addEventListener("loadedmetadata", r, { once: true }));
  stage.style.aspectRatio = `${video.videoWidth} / ${video.videoHeight}`;
  stage.classList.add("on"); resizeOverlay(); S.running = true;
  $("#camBtn").textContent = "Stop camera"; $("#camBtn").classList.replace("primary", "danger");
  $("#hudMode").textContent = "camera";
  return true;
}
function stopCamera() {
  S.stream?.getTracks().forEach((t) => t.stop());
  S.stream = null; video.srcObject = null; S.running = false; S.tracks = [];
  stage.classList.remove("on");
  $("#camBtn").textContent = "Start camera"; $("#camBtn").classList.replace("danger", "primary");
  $("#hudFaces").textContent = "Faces 0"; $("#hudMode").textContent = "idle";
}
$("#camBtn").onclick = () => (S.running ? stopCamera() : startCamera());

function resizeOverlay() {
  const r = stage.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
  if (!r.width) return;
  for (const c of [overlay, fx]) {
    c.width = Math.round(r.width * dpr); c.height = Math.round(r.height * dpr);
    c.style.width = r.width + "px"; c.style.height = r.height + "px";
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0); fctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}
new ResizeObserver(resizeOverlay).observe(stage);

function sendFrame() {
  const ws = S.ws;
  if (S.inflight && performance.now() - S.t0 > 4000) S.inflight = false;
  if (!S.running || S.inflight || !ws || ws.readyState !== 1 || video.readyState < 2) return;
  const s = Math.min(1, 640 / video.videoWidth);
  grab.width = Math.round(video.videoWidth * s); grab.height = Math.round(video.videoHeight * s);
  grab.getContext("2d").drawImage(video, 0, 0, grab.width, grab.height);
  S.inflight = true; S.t0 = performance.now();
  grab.toBlob((b) => { if (b && ws.readyState === 1) ws.send(b); else S.inflight = false; }, "image/jpeg", 0.72);
}
setInterval(sendFrame, 50);
setInterval(() => {
  const now = performance.now(), fps = S.frames / ((now - S.fpsLast) / 1000);
  $("#hudFps").textContent = (S.running ? fps.toFixed(0) : 0) + " fps"; S.frames = 0; S.fpsLast = now;
}, 1000);

/* ---------- animated face tracks ---------- */
const COLORS = { marked: "#22c55e", present: "#38bdf8", verifying: "#fbbf24", unknown: "#fb7185", liveness: "#c084fc", small: "#94a3b8", paused: "#94a3b8" };
function updateTracks(m) {
  const W = overlay.clientWidth, H = overlay.clientHeight, sx = W / m.w, sy = H / m.h, used = new Set();
  for (const f of m.faces) {
    const [x1, y1, x2, y2] = f.box;
    const box = [W - x2 * sx, y1 * sy, W - x1 * sx, y2 * sy];          // mirrored like the video
    const cx = (box[0] + box[2]) / 2, cy = (box[1] + box[3]) / 2;
    let best = null, bd = 1e9;
    for (const t of S.tracks) {
      if (used.has(t) || t.demo) continue;
      const d = Math.hypot(cx - (t.target[0] + t.target[2]) / 2, cy - (t.target[1] + t.target[3]) / 2);
      if (d < bd) { bd = d; best = t; }
    }
    let t = best && bd < Math.max(box[2] - box[0], 60) * 1.3 ? best : null;
    if (!t) { t = { box: box.slice(), born: performance.now(), flash: 0, prog: 0 }; S.tracks.push(t); }
    t.target = box; t.face = f; t.ttl = 8; used.add(t);
    if (f.status === "marked" && !t.celebrated) { t.flash = 1; t.celebrated = true; burst((box[0] + box[2]) / 2, (box[1] + box[3]) / 2, 36); }
  }
  for (const t of S.tracks) if (!t.demo && !used.has(t)) t.ttl--;
  S.tracks = S.tracks.filter((t) => t.demo || t.ttl > 0);
}

function labelFor(f) {
  const pct = Math.round((f.score || 0) * 100);
  switch (f.status) {
    case "marked": return `${f.name}  marked`;
    case "present": return `${f.name}  present`;
    case "verifying": return `${f.name}  ${Math.round((f.progress || 0) * 100)}%`;
    case "liveness": return `${f.name}  turn your head slightly`;
    case "paused": return `${f.name}  class ended`;
    case "small": return "Move closer";
    default: return f.score ? `Unknown  ${pct}%` : "Unknown";
  }
}
function roundRect(c, x, y, w, h, r) { c.beginPath(); c.roundRect ? c.roundRect(x, y, w, h, r) : c.rect(x, y, w, h); }
function drawTrack(t, now) {
  const f = t.face, col = COLORS[f.status] || "#94a3b8";
  const [x1, y1, x2, y2] = t.box, w = x2 - x1, h = y2 - y1, L = Math.min(w, h) * .26;
  if (t.demo) drawSilhouette(t, x1, y1, w, h);
  ctx.save();
  ctx.fillStyle = col + "14"; ctx.fillRect(x1, y1, w, h);
  if (f.status === "verifying" || f.status === "liveness") {            // scanning sweep inside the box
    const yy = y1 + (((now - t.born) / 900) % 1) * h;
    const g = ctx.createLinearGradient(0, yy - 18, 0, yy); g.addColorStop(0, col + "00"); g.addColorStop(1, col + "aa");
    ctx.fillStyle = g; ctx.fillRect(x1, yy - 18, w, 18);
  }
  ctx.strokeStyle = col; ctx.lineWidth = 3; ctx.lineCap = "round"; ctx.shadowColor = col; ctx.shadowBlur = 14;
  ctx.beginPath();
  ctx.moveTo(x1, y1 + L); ctx.lineTo(x1, y1); ctx.lineTo(x1 + L, y1);
  ctx.moveTo(x2 - L, y1); ctx.lineTo(x2, y1); ctx.lineTo(x2, y1 + L);
  ctx.moveTo(x2, y2 - L); ctx.lineTo(x2, y2); ctx.lineTo(x2 - L, y2);
  ctx.moveTo(x1 + L, y2); ctx.lineTo(x1, y2); ctx.lineTo(x1, y2 - L);
  ctx.stroke();
  ctx.shadowBlur = 0;
  if (t.flash > 0) {                                                    // expanding success ring
    const e = 1 - t.flash, pad = 8 + e * 60;
    ctx.globalAlpha = t.flash; ctx.lineWidth = 3; roundRect(ctx, x1 - pad, y1 - pad, w + pad * 2, h + pad * 2, 18 + e * 24); ctx.stroke();
    ctx.globalAlpha = 1; t.flash = Math.max(0, t.flash - .022);
  }
  if (f.status === "verifying" || f.status === "liveness") {            // progress bar
    const bw = Math.max(w, 70), bx = x1 + (w - bw) / 2;
    ctx.fillStyle = "#ffffff22"; roundRect(ctx, bx, y2 + 8, bw, 5, 3); ctx.fill();
    ctx.fillStyle = col; roundRect(ctx, bx, y2 + 8, Math.max(5, bw * (f.progress || 0)), 5, 3); ctx.fill();
  }
  const text = labelFor(f);
  ctx.font = "600 13px Inter, Segoe UI, sans-serif";
  const tw = ctx.measureText(text).width + 22, ty = y1 - 32 < 4 ? y2 + 18 : y1 - 32;
  ctx.fillStyle = col; roundRect(ctx, x1, ty, tw, 25, 8); ctx.fill();
  ctx.fillStyle = f.status === "unknown" || f.status === "marked" ? "#fff" : "#0b0f1f";
  ctx.textBaseline = "middle"; ctx.fillText(text, x1 + 11, ty + 13);
  ctx.restore();
}
function drawSilhouette(t, x, y, w, h) {
  const hu = hue(t.face.name || "x");
  ctx.save();
  ctx.fillStyle = `hsl(${hu} 55% 38%)`;
  ctx.beginPath(); ctx.ellipse(x + w / 2, y + h * .42, w * .36, h * .3, 0, 0, 7); ctx.fill();
  ctx.fillStyle = `hsl(${hu} 55% 28%)`;
  ctx.beginPath(); ctx.ellipse(x + w / 2, y + h * 1.05, w * .62, h * .34, 0, Math.PI, 0); ctx.fill();
  ctx.fillStyle = "#ffffff55"; const ey = y + h * .4;
  ctx.beginPath(); ctx.arc(x + w * .38, ey, w * .035, 0, 7); ctx.arc(x + w * .62, ey, w * .035, 0, 7); ctx.fill();
  ctx.restore();
}
function drawDemoRoom(now) {
  const W = overlay.clientWidth, H = overlay.clientHeight;
  const g = ctx.createRadialGradient(W / 2, H * .45, 20, W / 2, H / 2, W * .7);
  g.addColorStop(0, "#1b2552"); g.addColorStop(1, "#05070f");
  ctx.fillStyle = g; ctx.fillRect(0, 0, W, H);
  ctx.strokeStyle = "rgba(124,92,255,.18)"; ctx.lineWidth = 1;
  const off = (now / 40) % 40;
  for (let x = -40 + off; x < W; x += 40) { ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke(); }
  for (let y = -40 + off; y < H; y += 40) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke(); }
}

function frame(now) {
  requestAnimationFrame(frame);
  const W = overlay.clientWidth, H = overlay.clientHeight;
  ctx.clearRect(0, 0, W, H); fctx.clearRect(0, 0, W, H);
  if (S.demo) drawDemoRoom(now);
  for (const t of S.tracks) {
    if (t.demo && t.expire && now > t.expire) { t.dead = true; continue; }
    if (t.demo) demoStep(t, now);
    for (let i = 0; i < 4; i++) t.box[i] += (t.target[i] - t.box[i]) * .22;   // glide
    drawTrack(t, now);
  }
  S.tracks = S.tracks.filter((t) => !t.dead);
  if (S.demo) $("#hudFaces").textContent = `Faces ${S.tracks.length}`;
  drawParticles();
}

/* ---------- particle bursts ---------- */
const CONF = ["#22c55e", "#22d3ee", "#7c5cff", "#fbbf24", "#f472b6", "#ffffff"];
function burst(x, y, n = 30) {
  if (!S.prefs.confetti || S.prefs.calm) return;
  for (let i = 0; i < n; i++) {
    const a = Math.random() * 7, v = 2 + Math.random() * 5;
    S.particles.push({ x, y, vx: Math.cos(a) * v, vy: Math.sin(a) * v - 2, life: 1, r: 2 + Math.random() * 3, c: CONF[i % CONF.length], rot: Math.random() * 7 });
  }
}
function drawParticles() {
  for (const p of S.particles) {
    p.x += p.vx; p.y += p.vy; p.vy += .13; p.vx *= .985; p.life -= .014; p.rot += .2;
    fctx.globalAlpha = Math.max(0, p.life); fctx.fillStyle = p.c;
    fctx.save(); fctx.translate(p.x, p.y); fctx.rotate(p.rot); fctx.fillRect(-p.r, -p.r / 2, p.r * 2, p.r); fctx.restore();
  }
  fctx.globalAlpha = 1;
  S.particles = S.particles.filter((p) => p.life > 0);
}

/* ---------- demo mode (no webcam needed) ---------- */
async function startDemo() {
  if (S.running) stopCamera();
  S.demo = true; stage.classList.add("demo"); stage.style.aspectRatio = "16 / 9"; resizeOverlay();
  $("#demoBtn").textContent = "Stop demo"; $("#hudMode").textContent = "demo";
  if (!S.students.length) {
    try { await api("/api/demo/seed", { method: "POST" }); await loadStudents(true); toast("Loaded demo students", "success"); }
    catch (e) { toast(e.message, "error"); stopDemo(); return; }
  }
  S.demoTimer = setInterval(demoSpawn, 3600); demoSpawn();
}
function stopDemo() {
  S.demo = false; clearInterval(S.demoTimer); stage.classList.remove("demo");
  S.tracks = S.tracks.filter((t) => !t.demo);
  $("#demoBtn").textContent = "Demo mode"; $("#hudMode").textContent = "idle";
}
$("#demoBtn").onclick = () => (S.demo ? stopDemo() : startDemo());
function demoSpawn() {
  if (!S.demo || S.tracks.length > 2) return;
  const W = overlay.clientWidth, H = overlay.clientHeight, w = Math.min(150, W * .17), h = w * 1.2, col = W / 3;
  const taken = new Set(S.tracks.map((t) => Math.floor(((t.target[0] + t.target[2]) / 2) / col)));
  const free = [0, 1, 2].filter((c) => !taken.has(c));
  if (!free.length) return;
  const c = free[Math.floor(Math.random() * free.length)];
  const x = c * col + (col - w) / 2 + (Math.random() - .5) * col * .25, y = H * .2 + Math.random() * Math.max(10, H * .6 - h), box = [x, y, x + w, y + h];
  const pool = S.students.filter((s) => !s.today_status && !S.tracks.some((t) => t.face.student_id === s.id));
  const stranger = !pool.length || Math.random() < .14;
  const stu = pool[Math.floor(Math.random() * pool.length)];
  S.tracks.push({
    demo: true, box: box.map((v, i) => v + (i % 2 ? 0 : -50)), target: box, born: performance.now(), flash: 0, ttl: 99, phase: Math.random() * 7,
    face: stranger ? { name: "Unknown", status: "unknown", score: .21, progress: 0 }
      : { name: stu.name, roll_no: stu.roll_no, student_id: stu.id, status: "verifying", score: .8, progress: 0 },
  });
}
function demoStep(t, now) {
  const age = now - t.born, f = t.face;
  t.target = t.target.map((v, i) => v + Math.sin(now / 900 + t.phase + (i % 2)) * .15);
  if (f.status === "verifying") {
    f.progress = Math.min(1, age / 1900);
    if (f.progress >= 1 && !t.sent) {
      t.sent = true;
      api("/api/attendance/manual", { method: "POST", json: { student_id: f.student_id, source: "demo" } })
        .catch((e) => { toast(e.message, "error"); t.dead = true; });
    }
  } else if (f.status === "unknown" && age > 2400 && !t.expire) t.expire = now + 300;
}

/* ---------- welcome banner, sound, voice ---------- */
function speak(text) {
  try { if (S.prefs.voice && window.speechSynthesis) { speechSynthesis.cancel(); speechSynthesis.speak(new SpeechSynthesisUtterance(text)); } } catch { /* unsupported */ }
}
function chime() {
  if (!S.prefs.sound) return;
  try {
    const ac = chime.ac || (chime.ac = new (window.AudioContext || window.webkitAudioContext)());
    [660, 880].forEach((f, i) => {
      const o = ac.createOscillator(), g = ac.createGain(), t = ac.currentTime + i * .12;
      o.frequency.value = f; o.type = "sine"; g.gain.setValueAtTime(.0001, t); g.gain.exponentialRampToValueAtTime(.12, t + .02); g.gain.exponentialRampToValueAtTime(.0001, t + .35);
      o.connect(g).connect(ac.destination); o.start(t); o.stop(t + .4);
    });
  } catch { /* audio blocked */ }
}
async function pumpWelcome() {
  if (S.welcomeBusy) return;
  S.welcomeBusy = true;
  while (S.welcomeQ.length) {
    const m = S.welcomeQ.shift(), el = $("#welcome");
    el.classList.remove("show", "late"); void el.offsetWidth;
    $("#wAv").style.setProperty("--h", hue(m.name)); $("#wAv").textContent = initials(m.name);
    $("#wName").textContent = (m.status === "late" ? "Welcome, " : "Welcome, ") + m.name;
    $("#wSub").textContent = `${m.status === "late" ? "Marked late" : "Marked present"} - ${fmtTime(m.time)}${m.source === "face" ? ` - ${Math.round(m.confidence * 100)}% match` : ` - ${m.source}`}`;
    el.classList.toggle("late", m.status === "late"); el.classList.add("show");
    await sleep(2600); el.classList.remove("show"); await sleep(500);
  }
  S.welcomeBusy = false;
}

/* ---------- marks, feed, stats ---------- */
function feedItem(r) {
  if (r.unknown) {
    return `<li class="alert"><div class="av" style="--h:350">?</div><div class="who"><b>Unrecognised face</b><small>${esc(r.camera || "camera")}</small></div><div class="when">${fmtTime(r.time)}</div></li>`;
  }
  const late = r.status === "late";
  return `<li><div class="av" style="--h:${hue(r.name)}">${esc(initials(r.name))}</div>
    <div class="who"><b>${esc(r.name)}</b><small>${esc(r.roll_no)} - ${esc(r.department || "General")}${r.source && r.source !== "face" ? " - " + esc(r.source) : ""}</small></div>
    <div style="text-align:right"><span class="pill ${late ? "late" : "present"}">${late ? "Late" : "Present"}</span><div class="when">${fmtTime(r.time)}</div></div></li>`;
}
function addFeed(r, fresh = true) {
  const feed = $("#feed");
  feed.querySelector(".empty")?.remove();
  feed.insertAdjacentHTML("afterbegin", feedItem(r));
  if (fresh && !r.unknown) feed.firstElementChild.classList.add("fresh");
  while (feed.children.length > 40) feed.lastElementChild.remove();
  if (!r.unknown) $("#feedCount").textContent = ++S.feedCount;
}
function onMarked(m) {
  addFeed(m);
  const t = S.tracks.find((x) => x.face && x.face.student_id === m.student_id);
  if (t) { t.face.status = "marked"; t.flash = 1; if (!t.celebrated) { t.celebrated = true; burst((t.box[0] + t.box[2]) / 2, (t.box[1] + t.box[3]) / 2, 40); } if (t.demo) t.expire = performance.now() + 2800; }
  else burst(overlay.clientWidth / 2, overlay.clientHeight / 2, 24);
  S.welcomeQ.push(m); pumpWelcome();
  speak(`Welcome ${m.name}`); chime();
  $("#presentCard").classList.remove("bump"); void $("#presentCard").offsetWidth; $("#presentCard").classList.add("bump");
  loadStats(); if (S.view === "students") loadStudents(true);
}
function renderStats(st) {
  S.stats = st;
  countUp($("#stTotal"), st.total); countUp($("#stPresent"), st.present); countUp($("#stLate"), st.late);
  countUp($("#stAbsent"), st.absent); countUp($("#stRate"), st.rate, "%");
  $("#ringFg").setAttribute("stroke-dasharray", `${st.rate},100`);
}
async function loadStats() { try { renderStats(await api("/api/stats")); } catch { /* offline */ } }
async function loadFeed() {
  try {
    const rows = await api("/api/attendance");
    $("#feed").innerHTML = rows.length ? "" : '<li class="empty">Waiting for faces...</li>';
    S.feedCount = 0;
    rows.slice(0, 40).reverse().forEach((r) => addFeed(r, false));
    S.feedCount = rows.length; $("#feedCount").textContent = rows.length;
  } catch { /* offline */ }
}

/* ---------- analytics ---------- */
const chartTheme = () => ({ text: css("--muted"), grid: css("--line") });
function gradient(c, a, b) { const g = c.getContext("2d").createLinearGradient(0, 0, 0, 260); g.addColorStop(0, a); g.addColorStop(1, b); return g; }
function drawChart(id, cfg) {
  if (typeof Chart === "undefined") return;
  S.charts[id]?.destroy();
  const el = $("#" + id), th = chartTheme();
  Chart.defaults.color = th.text; Chart.defaults.font.family = "Inter, Segoe UI, sans-serif";
  cfg.options = Object.assign({ responsive: true, maintainAspectRatio: false, animation: { duration: 1100, easing: "easeOutQuart" },
    plugins: { legend: { display: false } },
    scales: { x: { grid: { display: false } }, y: { beginAtZero: true, ticks: { precision: 0 }, grid: { color: th.grid } } } }, cfg.options || {});
  S.charts[id] = new Chart(el, cfg);
}
function renderCharts(st) {
  const el1 = $("#weekChart"), el2 = $("#hourChart");
  drawChart("weekChart", { type: "bar", data: {
    labels: st.week.map((w) => new Date(w.day + "T00:00").toLocaleDateString([], { weekday: "short", day: "numeric" })),
    datasets: [{ data: st.week.map((w) => w.count), borderRadius: 10, backgroundColor: gradient(el1, "#7c5cff", "#22d3ee") }] } });
  const first = Math.min(8, st.hourly.findIndex((v) => v > 0) < 0 ? 8 : st.hourly.findIndex((v) => v > 0));
  const last = Math.max(16, st.hourly.length - 1 - [...st.hourly].reverse().findIndex((v) => v > 0));
  const hours = st.hourly.map((v, i) => [i, v]).slice(first, Math.min(24, last + 1));
  drawChart("hourChart", { type: "line", data: { labels: hours.map(([h]) => `${h}:00`),
    datasets: [{ data: hours.map(([, v]) => v), fill: true, tension: .4, borderColor: "#22d3ee", pointRadius: 3, pointBackgroundColor: "#22d3ee",
      backgroundColor: gradient(el2, "#22d3ee66", "#22d3ee00") }] } });
}
function refreshCharts() { if (S.view === "analytics" && S.stats) renderCharts(S.stats); }
function heatColor(p, count) { return !count ? css("--line") : p < 40 ? "#7c5cff55" : p < 70 ? "#7c5cffaa" : p < 90 ? "#22d3ee" : "#22c55e"; }
function bars(el, rows, opts = {}) {
  el.innerHTML = rows.length ? rows.map((r) => `<div class="bar-row"><span class="name" title="${esc(r.label)}">${esc(r.label)}</span>
    <div class="track ${opts.risk ? "risk" : ""}"><i data-w="${r.value}"></i>${opts.target ? `<b class="target" style="left:${opts.target}%"></b>` : ""}</div><span>${esc(r.text)}</span></div>`).join("")
    : `<div class="empty-note">${opts.empty || "No data yet"}</div>`;
  requestAnimationFrame(() => requestAnimationFrame(() => $$("i[data-w]", el).forEach((i) => (i.style.width = i.dataset.w + "%"))));
}
async function loadAnalytics() {
  try {
    const [st, heat, ins] = await Promise.all([api("/api/stats"), api("/api/heatmap"), api("/api/insights")]);
    renderStats(st); renderCharts(st);
    const lead = (new Date(heat[0].day + "T00:00").getDay() + 6) % 7;
    $("#heat").innerHTML = '<i style="visibility:hidden"></i>'.repeat(lead) + heat.map((d, i) => {
      const label = new Date(d.day + "T00:00").toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" });
      return `<i title="${label}: ${d.count ? d.percent + "% (" + d.count + " present)" : "no class"}" style="background:${heatColor(d.percent, d.count)};animation-delay:${i * 9}ms"></i>`;
    }).join("");
    $("#heatNote").textContent = `${heat.filter((d) => d.count).length} class days`;
    bars($("#deptBars"), st.departments.map((d) => ({ label: d.dept, value: d.total ? Math.round(100 * d.present / d.total) : 0, text: `${d.present}/${d.total}` })));
    $("#riskNote").textContent = `below ${ins.target}%`;
    bars($("#riskBars"), ins.at_risk.map((s) => ({ label: s.name, value: s.percent, text: s.percent + "%" })), { risk: true, target: ins.target, empty: "Everyone is on track" });
    const medals = ["1", "2", "3", "4", "5"];
    $("#punctual").innerHTML = ins.punctual.length ? ins.punctual.map((p, i) => `<li style="animation-delay:${i * 70}ms"><span class="medal">${["\u{1F947}", "\u{1F948}", "\u{1F949}"][i] || medals[i]}</span>${avatar(p.name)}<div style="flex:1"><b>${esc(p.name)}</b><br><small>${esc(p.roll_no)}</small></div><small>avg ${String(Math.floor(p.avg_min / 60)).padStart(2, "0")}:${String(p.avg_min % 60).padStart(2, "0")}</small></li>`).join("")
      : '<li class="empty-note" style="display:block">Needs a few days of data</li>';
  } catch (e) { toast(e.message, "error"); }
}

/* ---------- students ---------- */
async function loadStudents(silent = false) {
  try { S.students = await api("/api/students"); } catch (e) { if (!silent) toast(e.message, "error"); return; }
  const depts = [...new Set(S.students.map((s) => s.department || "General"))].sort(), sel = $("#deptFilter"), cur = sel.value;
  sel.innerHTML = '<option value="">All departments</option>' + depts.map((d) => `<option>${esc(d)}</option>`).join(""); sel.value = depts.includes(cur) ? cur : "";
  renderStudents();
}
function renderStudents() {
  const q = $("#search").value.trim().toLowerCase(), dept = $("#deptFilter").value;
  const rows = S.students.filter((s) => (!dept || (s.department || "General") === dept) && (!q || `${s.name} ${s.roll_no} ${s.department}`.toLowerCase().includes(q)));
  $("#peopleCount").textContent = rows.length; $("#noStudents").hidden = S.students.length > 0;
  const target = S.cfg.target || 75;
  $("#studentRows").innerHTML = rows.map((s, i) => {
    const st = s.today_status, pill = st ? `<span class="pill ${st}">${st === "late" ? "Late" : "Present"} ${fmtTime(s.today_time)}</span>` : '<span class="pill absent">Absent</span>';
    const pct = s.percent;
    return `<tr style="animation-delay:${Math.min(i, 14) * 35}ms"><td><div class="stu">${avatar(s.name)}<div><b>${esc(s.name)}</b><small>${esc(s.roll_no)}</small></div></div></td>
      <td>${esc(s.department || "General")}</td><td>${s.samples}</td>
      <td>${pct === null ? '<span class="muted">-</span>' : `<div class="pct"><div class="track ${pct < target ? "risk" : ""}"><i style="width:${pct}%"></i></div><b>${pct}%</b></div>`}</td>
      <td>${pill}</td>
      <td><div class="row-actions"><button class="btn sm ${st ? "danger" : "good"}" data-act="toggle" data-id="${s.id}">${st ? "Mark absent" : "Mark present"}</button>
      <button class="btn sm" data-act="samples" data-id="${s.id}" title="Add more face samples">+ Face</button>
      <button class="btn sm danger" data-act="del" data-id="${s.id}" title="Delete student and face data">Delete</button></div></td></tr>`;
  }).join("");
}
$("#search").oninput = renderStudents; $("#deptFilter").onchange = renderStudents;
$("#studentRows").onclick = async (e) => {
  const b = e.target.closest("button[data-act]"); if (!b) return;
  const s = S.students.find((x) => x.id == b.dataset.id);
  try {
    if (b.dataset.act === "toggle") { await api("/api/attendance/manual", { method: "POST", json: { student_id: s.id, present: !s.today_status } }); await loadStudents(true); loadStats(); }
    if (b.dataset.act === "samples") openEnroll(s);
    if (b.dataset.act === "del" && confirm(`Delete ${s.name} and all their face data? This cannot be undone.`)) { await api("/api/students/" + s.id, { method: "DELETE" }); toast(`${s.name} removed`, "success"); }
  } catch (err) { toast(err.message, "error"); }
};
const download = async (path, name) => {
  try { const blob = await (await api(path)).blob(), a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(blob), download: name }); a.click(); URL.revokeObjectURL(a.href); }
  catch (e) { toast(e.message, "error"); }
};
const isoDay = (d) => new Date(d - d.getTimezoneOffset() * 6e4).toISOString().slice(0, 10);
$("#exportBtn").onclick = () => download("/api/export", `attendance_${isoDay(new Date())}.csv`);
$("#exportRangeBtn").onclick = () => { const end = new Date(), start = new Date(end - 29 * 864e5); download(`/api/export/range?start=${isoDay(start)}&end=${isoDay(end)}`, "attendance_30_days.csv"); };

/* ---------- enrollment with guided capture ---------- */
const dlg = $("#enrollDlg"), evideo = $("#enrollVideo");
const STEPS = ["Look straight at the camera", "Turn slightly to your left", "Turn slightly to your right", "Lift your chin a little", "Relax and smile"];
async function openEnroll(stu = null) {
  S.enrollFor = stu; $("#shots").innerHTML = ""; $("#count").textContent = ""; $("#oval").classList.remove("ok");
  $("#prompt").textContent = "Fit your face inside the oval";
  $("#enrollTitle").textContent = stu ? `Add face samples - ${stu.name}` : "Enroll a student";
  for (const [id, v] of [["#fRoll", stu?.roll_no], ["#fName", stu?.name], ["#fDept", stu?.department], ["#fGuardian", stu?.guardian_email]]) { $(id).value = v || ""; $(id).disabled = !!stu; }
  $("#enrollGo").disabled = false; $("#enrollGo").textContent = stu ? "Capture samples" : "Capture and save";
  dlg.showModal();
  try { S.enrollStream = await navigator.mediaDevices.getUserMedia({ video: { width: 960, height: 720, facingMode: "user" }, audio: false }); evideo.srcObject = S.enrollStream; }
  catch (e) { toast("Camera unavailable: " + e.message, "error"); }
}
function closeEnroll() { S.enrollStream?.getTracks().forEach((t) => t.stop()); S.enrollStream = null; evideo.srcObject = null; if (dlg.open) dlg.close(); }
$("#enrollBtn").onclick = () => openEnroll(); $("#enrollCancel").onclick = closeEnroll; dlg.addEventListener("close", closeEnroll);
$("#enrollForm").onsubmit = async (e) => {
  e.preventDefault();
  if (!S.enrollStream || !evideo.videoWidth) return toast("Camera not ready", "warn");
  const go = $("#enrollGo"); go.disabled = true;
  const blobs = [], c = document.createElement("canvas"); c.width = evideo.videoWidth; c.height = evideo.videoHeight;
  $("#shots").innerHTML = "";
  for (let i = 0; i < STEPS.length; i++) {
    $("#prompt").textContent = `${i + 1}/${STEPS.length}  ${STEPS[i]}`;
    for (let n = 3; n >= 1; n--) { $("#count").textContent = n; await sleep(520); }
    $("#count").textContent = "";
    c.getContext("2d").drawImage(evideo, 0, 0);
    const blob = await new Promise((r) => c.toBlob(r, "image/jpeg", .92)); blobs.push(blob);
    $("#flash").classList.remove("go"); void $("#flash").offsetWidth; $("#flash").classList.add("go");
    $("#shots").insertAdjacentHTML("beforeend", `<img alt="" src="${URL.createObjectURL(blob)}">`);
  }
  $("#oval").classList.add("ok"); $("#prompt").textContent = "Analysing face data...";
  const fd = new FormData();
  blobs.forEach((b, i) => fd.append("images", b, `shot${i}.jpg`));
  if (!S.enrollFor) { fd.append("roll_no", $("#fRoll").value); fd.append("name", $("#fName").value); fd.append("department", $("#fDept").value); fd.append("guardian_email", $("#fGuardian").value); }
  try {
    const r = await api(S.enrollFor ? `/api/students/${S.enrollFor.id}/samples` : "/api/students", { method: "POST", body: fd });
    toast(`Saved ${r.faces_used} face sample(s)`, "success"); closeEnroll(); loadStudents(true); loadStats();
  } catch (err) { toast(err.message, "error"); $("#oval").classList.remove("ok"); $("#prompt").textContent = "Try again"; go.disabled = false; }
};

/* ---------- sessions ---------- */
const PRESETS = [["Lecture", 15], ["Lab", 10], ["Tutorial", 5], ["Exam", 0]];
$("#presets").innerHTML = PRESETS.map(([n, m]) => `<button type="button" class="btn sm" data-n="${n}" data-m="${m}">${n} - late after ${m} min</button>`).join("");
$("#presets").onclick = (e) => { const b = e.target.closest("button"); if (b) { $("#sName").value = b.dataset.n; $("#sLate").value = b.dataset.m; $("#sName").focus(); } };
function updateTimer() {
  const s = S.session; if (!s) { $("#curTimer").textContent = "--:--:--"; return; }
  const sec = Math.max(0, Math.floor((Date.now() - new Date(s.started_at)) / 1000));
  $("#curTimer").textContent = [sec / 3600, (sec / 60) % 60, sec % 60].map((v) => String(Math.floor(v)).padStart(2, "0")).join(":");
}
async function loadSessions() {
  try {
    const [list, cfg] = await Promise.all([api("/api/sessions"), api("/api/config")]);
    S.cfg = cfg; setSession(cfg.session);
    const s = cfg.session;
    $("#curName").textContent = s ? s.name : "No active class"; $("#endSessionBtn").hidden = !s;
    $("#curMeta").textContent = s ? `Started ${fmtTime(s.started_at)}${s.late_after_min != null ? ` - late after ${s.late_after_min} min` : ""}` : "Marking is paused. Start a class to resume.";
    updateTimer();
    $("#sessionList").innerHTML = list.map((x, i) => `<li style="animation-delay:${Math.min(i, 12) * 40}ms"><div><b>${esc(x.name)}</b> <small class="muted">${x.day} ${fmtTime(x.started_at)}${x.ended_at ? "" : " - live"}</small></div>
      <div><span class="pill present">${x.present - x.late} on time</span> <span class="pill late">${x.late} late</span></div></li>`).join("") || '<li class="empty-note" style="display:block">No classes yet</li>';
  } catch (e) { toast(e.message, "error"); }
}
$("#sessionForm").onsubmit = async (e) => {
  e.preventDefault();
  try { const late = $("#sLate").value; await api("/api/sessions", { method: "POST", json: { name: $("#sName").value, late_after_min: late === "" ? null : +late } }); $("#sName").value = ""; toast("Class started", "success"); loadSessions(); loadStats(); }
  catch (err) { toast(err.message, "error"); }
};
$("#endSessionBtn").onclick = async () => { try { await api(`/api/sessions/${S.session.id}/end`, { method: "POST" }); toast("Class ended", "success"); loadSessions(); } catch (e) { toast(e.message, "error"); } };

/* ---------- settings ---------- */
let patchTimer;
function patchCfg(body) { clearTimeout(patchTimer); patchTimer = setTimeout(() => api("/api/config", { method: "PATCH", json: body }).catch((e) => toast(e.message, "error")), 350); }
async function loadSettings() {
  try {
    const [cfg, health, events] = await Promise.all([api("/api/config"), api("/api/health"), api("/api/events")]);
    S.cfg = cfg;
    $("#thr").value = cfg.threshold; $("#thrVal").textContent = (+cfg.threshold).toFixed(2);
    $("#conf").value = cfg.confirm_frames; $("#confVal").textContent = cfg.confirm_frames;
    $("#liveSw").checked = cfg.liveness; $("#camName").value = S.prefs.camera;
    $("#soundSw").checked = S.prefs.sound; $("#calmSw").checked = S.prefs.calm; $("#confettiSw").checked = S.prefs.confetti;
    const up = health.uptime, rows = [["Backend", cfg.backend], ["Version", cfg.version], ["Threshold", (+cfg.threshold).toFixed(2)], ["Confirm frames", cfg.confirm_frames],
      ["Liveness", cfg.liveness ? "on" : "off"], ["Admin login", cfg.auth_required ? "required" : "open mode"], ["Uptime", `${Math.floor(up / 3600)}h ${Math.floor(up / 60) % 60}m`]];
    $("#sysInfo").innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join("");
    $("#eventLog").innerHTML = events.map((ev) => `<li><span><time>${ev.ts.slice(11, 16)}</time>${esc(ev.message)}</span><span class="pill none">${esc(ev.kind)}</span></li>`).join("") || '<li class="empty-note" style="display:block">No activity yet</li>';
  } catch (e) { toast(e.message, "error"); }
}
$("#thr").oninput = (e) => { $("#thrVal").textContent = (+e.target.value).toFixed(2); patchCfg({ match_threshold: +e.target.value }); };
$("#conf").oninput = (e) => { $("#confVal").textContent = e.target.value; patchCfg({ confirm_frames: +e.target.value }); };
$("#liveSw").onchange = (e) => patchCfg({ liveness: e.target.checked });
$("#camName").onchange = (e) => { S.prefs.camera = e.target.value.trim() || "Entrance"; savePrefs(); S.ws?.close(); toast("Camera name updated", "success"); };
$("#soundSw").onchange = (e) => { S.prefs.sound = e.target.checked; savePrefs(); chime(); };
$("#calmSw").onchange = (e) => { S.prefs.calm = e.target.checked; savePrefs(); applyPrefs(); };
$("#confettiSw").onchange = (e) => { S.prefs.confetti = e.target.checked; savePrefs(); };
$("#notifyBtn").onclick = async () => {
  try {
    const r = await api("/api/notify/absentees", { method: "POST" });
    $("#notifyOut").innerHTML = r.results.map((x) => `<li><span>${esc(x.student)} - ${esc(x.to)}</span><span class="pill ${x.status === "sent" ? "present" : "none"}">${esc(x.status)}</span></li>`).join("") || `<li class="empty-note" style="display:block">${r.absent} absent, no guardian e-mails on file</li>`;
    toast(`Alerts processed for ${r.absent} absent student(s)`, "success");
  } catch (e) { toast(e.message, "error"); }
};
$("#seedBtn").onclick = async () => { try { const r = await api("/api/demo/seed", { method: "POST" }); toast(`Added ${r.created} demo students`, "success"); loadStudents(true); loadStats(); } catch (e) { toast(e.message, "error"); } };
$("#clearDemoBtn").onclick = async () => { try { const r = await api("/api/demo", { method: "DELETE" }); toast(`Removed ${r.removed} demo students`, "success"); loadStudents(true); loadStats(); loadFeed(); } catch (e) { toast(e.message, "error"); } };

/* ---------- admin login ---------- */
function updateAuthBtn() {
  const b = $("#authBtn"); b.hidden = !S.cfg.auth_required;
  b.textContent = S.token ? "Log out" : "Admin login";
}
function openLogin() { if (!$("#loginDlg").open) { $("#loginDlg").showModal(); $("#pw").focus(); } }
$("#authBtn").onclick = () => { if (S.token) { S.token = ""; store.set("token", ""); updateAuthBtn(); toast("Logged out"); } else openLogin(); };
$("#loginCancel").onclick = () => $("#loginDlg").close();
$("#loginForm").onsubmit = async (e) => {
  e.preventDefault();
  try { const r = await fetch("/api/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ password: $("#pw").value }) });
    if (!r.ok) throw new Error("Wrong password");
    S.token = (await r.json()).token; store.set("token", S.token); $("#pw").value = ""; $("#loginDlg").close(); updateAuthBtn(); toast("Signed in", "success"); }
  catch (err) { toast(err.message, "error"); }
};

/* ---------- command palette + shortcuts ---------- */
const pal = $("#palette"); let palSel = 0, palItems = [];
function palCommands() {
  const cmds = [["Go to Live", () => show("live")], ["Go to Insights", () => show("analytics")], ["Go to People", () => show("students")], ["Go to Classes", () => show("sessions")],
    ["Go to Control", () => show("settings")], [S.running ? "Stop camera" : "Start camera", () => (S.running ? stopCamera() : startCamera())], [S.demo ? "Stop demo mode" : "Start demo mode", () => (S.demo ? stopDemo() : startDemo())],
    ["Toggle theme", () => $("#themeBtn").click()], ["Enroll a student", () => { show("students"); openEnroll(); }], ["Export today's CSV", () => $("#exportBtn").click()]];
  S.students.forEach((s) => cmds.push([`Student: ${s.name} (${s.roll_no})`, () => { show("students"); $("#search").value = s.roll_no; renderStudents(); }]));
  return cmds;
}
function palRender() {
  const q = $("#palInput").value.toLowerCase();
  palItems = palCommands().filter(([n]) => n.toLowerCase().includes(q)).slice(0, 9); palSel = Math.min(palSel, Math.max(0, palItems.length - 1));
  $("#palList").innerHTML = palItems.map(([n], i) => `<li class="${i === palSel ? "sel" : ""}" data-i="${i}">${esc(n)}</li>`).join("") || '<li>No matches</li>';
}
function openPalette() { $("#palInput").value = ""; palSel = 0; palRender(); pal.showModal(); $("#palInput").focus(); }
$("#paletteBtn").onclick = openPalette;
$("#palInput").oninput = () => { palSel = 0; palRender(); };
$("#palInput").onkeydown = (e) => {
  if (e.key === "ArrowDown") { palSel = (palSel + 1) % Math.max(1, palItems.length); palRender(); e.preventDefault(); }
  if (e.key === "ArrowUp") { palSel = (palSel - 1 + palItems.length) % Math.max(1, palItems.length); palRender(); e.preventDefault(); }
  if (e.key === "Enter" && palItems[palSel]) { pal.close(); palItems[palSel][1](); }
};
$("#palList").onclick = (e) => { const li = e.target.closest("li[data-i]"); if (li) { pal.close(); palItems[+li.dataset.i][1](); } };
pal.addEventListener("click", (e) => { if (e.target === pal) pal.close(); });
addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); openPalette(); return; }
  if (/input|select|textarea/i.test(document.activeElement?.tagName) || document.querySelector("dialog[open]")) return;
  const v = { 1: "live", 2: "analytics", 3: "students", 4: "sessions", 5: "settings" }[e.key]; if (v) show(v);
});

/* ---------- boot ---------- */
applyPrefs(); tickClock(); connect();
requestAnimationFrame(drawBg); requestAnimationFrame(frame);
Promise.all([loadStats(), loadFeed(), loadStudents(true), api("/api/config").then((c) => { S.cfg = c; setSession(c.session); updateAuthBtn(); }).catch(() => {})]);
