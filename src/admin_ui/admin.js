/* Hapilot admin panel: agent graph, custom nodes, prompt editor, audit log. No dependencies.
   Data: /hapilot/admin/api/* (src/ui_admin.py → src/admin.py). All text goes into the DOM as text, never as HTML. */
(() => {
  "use strict";

  const API = "/hapilot/admin/api";
  const SVG = "http://www.w3.org/2000/svg";
  const CARD = { w: 228, h: 112, gap: 34 };
  const KIND = { core: "Rdzeń", extension: "Rozszerzenie", custom: "Własny" };
  // nodes shipped with Hapilot are "built-in"; "Rozszerzenie" only for extensions added from outside the repository
  const kindLabel = (n) => (n.kind === "extension" && n.builtin ? "Wbudowany" : KIND[n.kind] || n.kind);
  const ACTIONS = {
    node_created: ["✨", "Dodano węzeł"], node_deleted: ["🗑️", "Usunięto węzeł"],
    prompt_saved: ["✏️", "Zmieniono prompt"], prompt_restored: ["↩️", "Przywrócono prompt"],
    node_updated: ["🛠️", "Zmieniono węzeł"], node_shared: ["🌐", "Udostępniono wszystkim"], node_unshared: ["🔒", "Wycofano udostępnienie"],
  };
  const isAdmin = () => Boolean(state.graph && state.graph.admin);
  const customClass = (n) => (n.kind === "custom" ? (n.global ? " global" : " private") : "");
  const PROMPT_TEMPLATE = "Jesteś wyspecjalizowanym asystentem firmy. Twoje zadanie: …\n\nZasady:\n- Odpowiadaj po polsku, rzeczowo i zwięźle.\n- Korzystaj z wyników wcześniejszych kroków, jeśli są podane.\n- Jeśli brakuje danych, napisz wprost, czego brakuje.\n";

  const state = { graph: null, layout: null, selected: null, prompts: [], promptsLoaded: false, role: null,
                  info: null, original: "", dirty: false, mode: "edit", view: { x: 0, y: 0, k: 1 }, fresh: null };

  // ------------------------------------------------------------------ helpers
  const $ = (sel, root = document) => root.querySelector(sel);

  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") el.className = value;
      else if (key === "style" && typeof value === "object") Object.assign(el.style, value);
      else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
      else if (key === "dataset") Object.assign(el.dataset, value);
      else el.setAttribute(key, value === true ? "" : value);
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      el.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return el;
  }

  function s(tag, attrs, ...children) {
    const el = document.createElementNS(SVG, tag);
    for (const [key, value] of Object.entries(attrs || {})) if (value !== null && value !== undefined) el.setAttribute(key, value);
    for (const child of children.flat()) if (child) el.append(child instanceof Node ? child : document.createTextNode(String(child)));
    return el;
  }

  async function api(path, options = {}) {
    const init = { credentials: "same-origin", ...options, headers: { "X-Hapilot-Admin": "1", ...(options.headers || {}) } };
    if (options.body !== undefined) { init.body = JSON.stringify(options.body); init.headers["Content-Type"] = "application/json"; }
    const res = await fetch(API + path, init);
    if (res.status === 401) { location.href = "/login"; throw new Error("Zaloguj się ponownie."); }
    let data = null;
    try { data = await res.json(); } catch (e) { /* empty body */ }
    if (!res.ok) throw new Error((data && data.error) || `Błąd ${res.status}`);
    return data;
  }

  function toast(text, kind = "") {
    const el = h("div", { class: `toast ${kind}` }, text);
    $("#toasts").append(el);
    setTimeout(() => { el.style.transition = "opacity .3s"; el.style.opacity = "0"; setTimeout(() => el.remove(), 300); }, kind === "error" ? 7000 : 3800);
  }

  function splitLabel(label, fallback = "🔹") {
    const m = /^(\p{Extended_Pictographic}(?:️|‍\p{Extended_Pictographic})*)\s*(.*)$/u.exec(label || "");
    return m ? [m[1], m[2] || label] : [fallback, label || ""];
  }

  const fmtDate = (iso) => { try { return new Date(iso).toLocaleString("pl-PL", { dateStyle: "medium", timeStyle: "short" }); } catch (e) { return iso; } };
  const plural = (n, one, few, many) => `${n} ${n === 1 ? one : (n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 12 || n % 100 > 14)) ? few : many}`;

  function slugify(text) {
    const map = { ą: "a", ć: "c", ę: "e", ł: "l", ń: "n", ó: "o", ś: "s", ź: "z", ż: "z" };
    let out = (text || "").toLowerCase().replace(/[ąćęłńóśźż]/g, (c) => map[c])
      .replace(/\p{Extended_Pictographic}/gu, "").replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
    if (out && !/^[a-z]/.test(out)) out = "w_" + out;
    return out.slice(0, 41);
  }

  // ------------------------------------------------------------------ modal
  function openModal(content, narrow = false) {
    const modal = $("#modal");
    modal.className = "modal" + (narrow ? " narrow" : "");
    modal.replaceChildren(content);
    $("#modal-backdrop").hidden = false;
    const first = modal.querySelector("input, textarea, select, button.btn");
    if (first) setTimeout(() => first.focus(), 30);
  }
  const closeModal = () => { $("#modal-backdrop").hidden = true; $("#modal").replaceChildren(); };

  function confirmDialog({ title, text, confirmLabel = "Potwierdź", danger = false }) {
    return new Promise((resolve) => {
      const done = (value) => { closeModal(); resolve(value); };
      openModal(h("div", {},
        h("div", { class: "modal-head" }, h("div", {}, h("h3", {}, title))),
        h("div", { class: "modal-body" }, h("p", { style: { margin: 0 } }, text)),
        h("div", { class: "modal-foot" },
          h("button", { class: "btn", onclick: () => done(false) }, "Anuluj"),
          h("button", { class: `btn ${danger ? "danger" : "primary"}`, onclick: () => done(true) }, confirmLabel))), true);
    });
  }

  // ------------------------------------------------------------------ graph layout
  // Org-chart layout: planner and router on the axis; tool nodes in one row, or two staggered rows when there
  // are many (row 2 sits half a step to the right, so every vertical connector runs through a gap between cards).
  // Router → nodes and nodes → "next step" use shared horizontal buses with rounded right-angle connectors.
  function computeLayout(data) {
    const byId = Object.fromEntries(data.nodes.map((n) => [n.id, n]));
    const workers = data.nodes.filter((n) => !["task_planner", "router"].includes(n.id));
    const rank = (n) => (n.kind === "custom" ? 2 : n.id === "hitl_confirm" ? 1 : 0);
    workers.sort((a, b) => rank(a) - rank(b));  // stable: server order within a group
    const pitch = CARD.w + CARD.gap;
    let perRow = workers.length > 7 ? Math.ceil(workers.length / 2) : workers.length;
    // never split a node from the node it always continues with (e.g. coder → execute) across the rows
    if (perRow < workers.length && workers[perRow - 1].then === workers[perRow].id) perRow += 1;
    const rows = [workers.slice(0, perRow), workers.slice(perRow)].filter((r) => r.length);
    const rowY = [560, 560 + CARD.h + 78];
    const rowW = Math.max(...rows.map((r, i) => r.length * pitch - CARD.gap + (i ? pitch / 2 : 0)));
    const cx = rowW / 2;
    const pos = {};
    const box = (id, x, y, w, h, extra = {}) => { pos[id] = { id, x, y, w, h, ...extra }; };
    box("__start__", cx - 64, 0, 128, 42, { terminal: "start", label: "▶ Start" });
    box("task_planner", cx - CARD.w / 2, 112, CARD.w, CARD.h);
    box("router", cx - CARD.w / 2, 300, CARD.w, CARD.h);
    rows.forEach((row, r) => row.forEach((n, i) => box(n.id, i * pitch + (r ? pitch / 2 : 0), rowY[r], CARD.w, CARD.h, { row: r })));
    const lastRowBottom = rowY[rows.length - 1] + CARD.h;
    const busTop = rowY[0] - 54, busBottom = lastRowBottom + 52;
    box("__next__", cx - 128, busBottom + 58, 256, 46, { terminal: "hub", label: "⤳ następny krok planu" });
    box("__end__", cx - 64, busBottom + 200, 128, 42, { terminal: "end", label: "■ Koniec" });
    const lanes = [
      { title: "Planowanie i rozdział kroków", y: 84, h: 340 },
      { title: "Węzły narzędzi", y: busTop - 30, h: lastRowBottom - busTop + 58 },
      { title: "Po kroku", y: busBottom + 36, h: 236 },
    ];
    return { pos, byId, workers, rowW, cx, lanes, busTop, busBottom, minX: -90, maxX: rowW + 90, height: busBottom + 260 };
  }

  const top = (p, f = 0.5) => ({ x: p.x + p.w * f, y: p.y });
  const bottom = (p, f = 0.5) => ({ x: p.x + p.w * f, y: p.y + p.h });
  const left = (p) => ({ x: p.x, y: p.y + p.h / 2 });
  const right = (p) => ({ x: p.x + p.w, y: p.y + p.h / 2 });
  const vcurve = (a, b) => { const my = (a.y + b.y) / 2; return `M${a.x},${a.y} C${a.x},${my} ${b.x},${my} ${b.x},${b.y}`; };

  // A polyline with rounded corners (radius r) through the given points.
  function rounded(points, r = 14) {
    let d = `M${points[0].x},${points[0].y}`;
    for (let i = 1; i < points.length - 1; i++) {
      const p = points[i - 1], c = points[i], n = points[i + 1];
      const d1 = Math.hypot(c.x - p.x, c.y - p.y), d2 = Math.hypot(n.x - c.x, n.y - c.y);
      const k = Math.min(r, d1 / 2, d2 / 2);
      if (k < 0.5) { d += ` L${c.x},${c.y}`; continue; }
      const a = { x: c.x - ((c.x - p.x) / d1) * k, y: c.y - ((c.y - p.y) / d1) * k };
      const b = { x: c.x + ((n.x - c.x) / d2) * k, y: c.y + ((n.y - c.y) / d2) * k };
      d += ` L${a.x},${a.y} Q${c.x},${c.y} ${b.x},${b.y}`;
    }
    const last = points[points.length - 1];
    return d + ` L${last.x},${last.y}`;
  }

  function edgePath(e, L) {
    const a = L.pos[e.source], b = L.pos[e.target];
    if (!a || !b) return null;
    switch (e.kind) {
      case "stop": {
        const p = left(a), q = left(b), x = L.minX - 30;
        return rounded([p, { x, y: p.y }, { x, y: q.y }, q], 40);
      }
      case "loop": {
        const p = right(a), q = right(b), x = L.maxX + 40;
        return rounded([p, { x, y: p.y }, { x, y: q.y }, q], 40);
      }
      case "route": {
        const p = bottom(a), q = top(b);
        return rounded([p, { x: p.x, y: L.busTop }, { x: q.x, y: L.busTop }, q]);
      }
      case "next": {
        const p = bottom(a), q = top(b);
        return rounded([p, { x: p.x, y: L.busBottom }, { x: q.x, y: L.busBottom }, q]);
      }
      case "then": {
        if (Math.abs(a.y - b.y) < 2 && b.x > a.x && b.x - (a.x + a.w) < CARD.gap + 4) {
          const p = right(a), q = left(b);
          return `M${p.x},${p.y - 14} L${q.x},${q.y - 14}`;
        }
        const p = top(a, 0.7), q = top(b, 0.3), y = Math.min(a.y, b.y) - 26;
        return rounded([p, { x: p.x, y }, { x: q.x, y }, q], 10);
      }
      case "retry": {
        if (Math.abs(a.y - b.y) < 2 && Math.abs(b.x - a.x) < CARD.w + CARD.gap + 4) {
          const p = left(a), q = right(b);
          return `M${p.x},${p.y + 16} L${q.x},${q.y + 16}`;
        }
        const p = bottom(a, 0.3), q = bottom(b, 0.7), y = Math.max(a.y + a.h, b.y + b.h) + 24;
        return rounded([p, { x: p.x, y }, { x: q.x, y }, q], 10);
      }
      default:
        return vcurve(bottom(a), top(b));
    }
  }

  // ------------------------------------------------------------------ graph rendering
  function overriddenRoles() { return new Set(state.prompts.filter((p) => p.overridden).map((p) => p.role)); }

  function nodeCard(n, extraClass = "") {
    const [icon, title] = splitLabel(n.label, n.kind === "custom" ? "🟣" : "🔹");
    const changed = overriddenRoles();
    const chips = [h("span", { class: "chip kind" }, n.kind === "custom" ? (n.global ? "🌐 Globalny" : "🔒 Prywatny") : kindLabel(n))];
    if (n.kind === "custom" && n.owner && !n.mine) chips.push(h("span", { class: "chip" }, "👤 " + n.owner));
    for (const t of (n.tools || []).slice(0, n.kind === "custom" ? 1 : 2)) chips.push(h("span", { class: "chip" }, "🛠 " + t.name));
    if ((n.prompts || []).some((r) => changed.has(r))) chips.push(h("span", { class: "chip mod", title: "Prompt zmieniony przez administratora" }, "✏️ prompt"));
    return h("div", { class: `node ${n.kind}${customClass(n)} ${extraClass}`, dataset: { id: n.id }, title: n.description || "" },
      h("div", { class: "node-head" }, h("div", { class: "node-icon" }, icon),
        h("div", { class: "node-title" }, h("b", {}, title), h("code", {}, n.id))),
      h("div", { class: "node-foot" }, chips));
  }

  function renderGraph() {
    const data = state.graph;
    const L = (state.layout = computeLayout(data));
    const defs = $("#graph-defs"), lanes = $("#lanes"), edges = $("#edges"), labels = $("#edge-labels"), nodes = $("#nodes");
    for (const g of [defs, lanes, edges, labels, nodes]) g.replaceChildren();

    for (const kind of ["route", "always", "then", "retry", "next", "loop", "stop", "custom"]) {
      defs.append(s("marker", { id: `arrow-${kind}`, viewBox: "0 0 10 10", refX: "8.5", refY: "5", markerWidth: "7", markerHeight: "7", orient: "auto-start-reverse" },
        s("path", { d: "M0,0 L10,5 L0,10 z", class: `arrowhead ${kind}` })));
    }
    for (const lane of L.lanes) {
      lanes.append(s("g", { class: "lane" },
        s("rect", { x: L.minX - 40, y: lane.y, width: L.maxX - L.minX + 80, height: lane.h, rx: 22 }),
        s("text", { x: L.minX - 18, y: lane.y + 26 }, lane.title)));
    }

    for (const e of data.edges) {
      const d = edgePath(e, L);
      if (!d) continue;
      const custom = (L.byId[e.source] || {}).kind === "custom" || (L.byId[e.target] || {}).kind === "custom";
      const cls = `edge ${e.kind}${custom && ["route", "next"].includes(e.kind) ? (L.byId[e.source]?.kind === "custom" ? " custom-out" : " custom-in") : ""}`;
      const marker = custom && ["route", "next"].includes(e.kind) ? "custom" : e.kind;
      const path = s("path", { d, class: cls, "marker-end": `url(#arrow-${marker})`, "data-s": e.source, "data-t": e.target },
        s("title", {}, `${nodeName(e.source)} → ${nodeName(e.target)}${e.label ? " — " + e.label : ""}`));
      edges.append(path);
      const tight = (e.kind === "then" || e.kind === "retry") && d.indexOf("Q") < 0;  // between adjacent cards: no room
      if (e.label && e.kind !== "route" && e.kind !== "next" && !tight) {
        const total = path.getTotalLength ? path.getTotalLength() : 0;
        const pt = total ? path.getPointAtLength(total / 2) : { x: 0, y: 0 };
        const text = s("text", { x: pt.x, y: pt.y + 4, "text-anchor": "middle" }, e.label);
        const group = s("g", { class: `edge-label ${e.kind}`, "data-s": e.source, "data-t": e.target }, s("rect", {}), text);
        labels.append(group);
        const bb = text.getBBox();
        const rect = group.querySelector("rect");
        rect.setAttribute("x", bb.x - 8); rect.setAttribute("y", bb.y - 4);
        rect.setAttribute("width", bb.width + 16); rect.setAttribute("height", bb.height + 8); rect.setAttribute("rx", 9);
      }
    }

    for (const [id, p] of Object.entries(L.pos)) {
      const fo = s("foreignObject", { x: p.x, y: p.y, width: p.w, height: p.h, class: "node-fo" });
      if (p.terminal) {
        fo.append(h("div", { class: `terminal ${p.terminal}` }, p.label));
      } else {
        const n = L.byId[id];
        const card = nodeCard(n, (state.selected === id ? "selected " : "") + (state.fresh === id ? "fresh" : ""));
        card.addEventListener("mouseenter", () => focusNode(id));
        card.addEventListener("mouseleave", () => focusNode(state.selected));
        card.addEventListener("click", (ev) => { ev.stopPropagation(); selectNode(id); });
        fo.append(card);
      }
      nodes.append(fo);
    }
    state.fresh = null;
    renderStats();
    focusNode(state.selected);
    $("#graph-loading").hidden = true;
  }

  function focusNode(id) {
    const svg = $("#graph");
    svg.classList.toggle("focus", Boolean(id));
    for (const el of svg.querySelectorAll(".edge, .edge-label")) {
      el.classList.toggle("hl", Boolean(id) && (el.dataset.s === id || el.dataset.t === id));
    }
  }

  function renderStats() {
    const nodes = state.graph.nodes;
    const workers = nodes.filter((n) => !["task_planner", "router"].includes(n.id));
    const mine = nodes.filter((n) => n.kind === "custom" && n.mine).length;
    const shared = nodes.filter((n) => n.kind === "custom" && n.global).length;
    const tools = nodes.reduce((sum, n) => sum + (n.tools || []).length, 0);
    const changed = state.prompts.filter((p) => p.overridden).length;
    $("#stats").replaceChildren(
      h("span", { class: "stat" }, h("b", {}, workers.length), " węzłów narzędzi"),
      h("span", { class: "stat" }, h("b", {}, mine), " moich"),
      h("span", { class: "stat" }, h("b", {}, shared), " globalnych"),
      h("span", { class: "stat" }, h("b", {}, tools), " narzędzi planera"),
      h("span", { class: "stat" }, h("b", {}, changed), " zmienionych promptów"));
  }

  // ------------------------------------------------------------------ pan and zoom
  function applyView() {
    const { x, y, k } = state.view;
    $("#viewport").setAttribute("transform", `translate(${x},${y}) scale(${k})`);
  }

  function fit(animate = false) {
    const L = state.layout;
    if (!L) return;
    const svg = $("#graph").getBoundingClientRect();
    const inspector = $("#inspector").classList.contains("open") ? 0 : 0;
    const bw = L.maxX - L.minX + 160, bh = L.height + 20;
    const k = Math.max(0.25, Math.min(1.25, Math.min((svg.width - inspector - 40) / bw, (svg.height - 90) / bh)));
    const target = { k, x: (svg.width - inspector - bw * k) / 2 - (L.minX - 80) * k, y: 70 + ((svg.height - 90) - bh * k) / 2 };
    if (!animate) { state.view = target; applyView(); return; }
    const from = { ...state.view }, t0 = performance.now();
    const step = (t) => {
      const f = Math.min(1, (t - t0) / 260), e = 1 - Math.pow(1 - f, 3);
      state.view = { x: from.x + (target.x - from.x) * e, y: from.y + (target.y - from.y) * e, k: from.k + (target.k - from.k) * e };
      applyView();
      if (f < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  function zoomAt(factor, cx, cy) {
    const v = state.view, k = Math.max(0.2, Math.min(2.5, v.k * factor));
    state.view = { k, x: cx - (cx - v.x) * (k / v.k), y: cy - (cy - v.y) * (k / v.k) };
    applyView();
  }

  function setupPanZoom() {
    const canvas = $("#canvas"), svg = $("#graph");
    svg.addEventListener("wheel", (ev) => {
      ev.preventDefault();
      const r = svg.getBoundingClientRect();
      zoomAt(Math.exp(-ev.deltaY * (ev.ctrlKey ? 0.01 : 0.0015)), ev.clientX - r.left, ev.clientY - r.top);
    }, { passive: false });
    let drag = null;
    svg.addEventListener("pointerdown", (ev) => {
      if (ev.target.closest(".node")) return;
      drag = { x: ev.clientX, y: ev.clientY, vx: state.view.x, vy: state.view.y, moved: false };
      canvas.classList.add("panning");
      svg.setPointerCapture(ev.pointerId);
    });
    svg.addEventListener("pointermove", (ev) => {
      if (!drag) return;
      const dx = ev.clientX - drag.x, dy = ev.clientY - drag.y;
      if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
      state.view.x = drag.vx + dx; state.view.y = drag.vy + dy; applyView();
    });
    svg.addEventListener("pointerup", () => {
      if (drag && !drag.moved) selectNode(null);
      drag = null; canvas.classList.remove("panning");
    });
    const center = () => { const r = svg.getBoundingClientRect(); return [r.width / 2, r.height / 2]; };
    $("#zoom-in").onclick = () => zoomAt(1.2, ...center());
    $("#zoom-out").onclick = () => zoomAt(1 / 1.2, ...center());
    $("#zoom-fit").onclick = () => fit(true);
    window.addEventListener("resize", () => fit());
  }

  // ------------------------------------------------------------------ inspector
  function selectNode(id) {
    state.selected = id;
    for (const el of document.querySelectorAll(".node[data-id]")) el.classList.toggle("selected", el.dataset.id === id);
    focusNode(id);
    if (!id) { $("#inspector").classList.remove("open"); $("#inspector").setAttribute("aria-hidden", "true"); return; }
    renderInspector(state.layout.byId[id]);
    $("#inspector").classList.add("open");
    $("#inspector").setAttribute("aria-hidden", "false");
    revealNode(id);
  }

  // Pan (animated) so a node is fully visible in the canvas next to the inspector.
  function revealNode(id) {
    const card = document.querySelector(`.node[data-id="${CSS.escape(id)}"]`);
    if (!card) return;
    // the visible area once the inspector has slid in (its final width, not the width mid-animation)
    const view = $('.view[data-view="graph"]').getBoundingClientRect();
    const docked = window.innerWidth > 900 && $("#inspector").classList.contains("open");
    const area = { left: view.left, top: view.top, right: view.right - (docked ? 420 : 0), bottom: view.bottom };
    const box = card.getBoundingClientRect(), pad = 48;
    let dx = 0, dy = 0;
    if (box.right > area.right - pad) dx = area.right - pad - box.right;
    if (box.left + dx < area.left + pad) dx = area.left + pad - box.left;
    if (box.bottom > area.bottom - pad) dy = area.bottom - pad - box.bottom;
    if (box.top + dy < area.top + pad) dy = area.top + pad - box.top;
    if (!dx && !dy) return;
    if (document.hidden) { state.view = { ...state.view, x: state.view.x + dx, y: state.view.y + dy }; applyView(); return; }
    const from = { ...state.view }, t0 = performance.now();
    const step = (t) => {
      const f = Math.min(1, (t - t0) / 260), e = 1 - Math.pow(1 - f, 3);
      state.view = { ...from, x: from.x + dx * e, y: from.y + dy * e };
      applyView();
      if (f < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  function nodeName(id) {
    if (id === "__end__") return "■ Koniec";
    if (id === "__next__") return "⤳ następny krok planu";
    if (id === "__start__") return "▶ Start";
    return (state.layout.byId[id] || {}).label || id;
  }

  function renderInspector(n) {
    const [icon, title] = splitLabel(n.label, n.kind === "custom" ? "🟣" : "🔹");
    const body = $("#inspector-body");
    const changed = overriddenRoles();
    const sections = [];
    sections.push(h("div", { class: `insp-head ${n.kind}${customClass(n)}` },
      h("div", { class: "insp-icon" }, icon), h("h3", {}, title), h("code", {}, n.id),
      h("div", { class: "chips" },
        h("span", { class: "chip kind" }, n.kind === "custom" ? (n.global ? "🌐 Globalny — dla wszystkich" : "🔒 Prywatny — tylko Twoje rozmowy") : kindLabel(n)),
        n.kind === "custom" ? h("span", { class: "chip" }, "👤 " + (n.mine ? "Twój węzeł" : n.owner)) : h("span", { class: "chip" }, "📦 " + n.extension_name))));
    if (n.description) sections.push(h("div", { class: "insp-section" }, h("h4", {}, "Opis"), h("p", {}, n.description)));
    if (n.kind === "custom") {
      const cfg = n.config || {};
      sections.push(h("div", { class: "insp-section" }, h("h4", {}, "Ustawienia węzła"),
        h("dl", { class: "kv" },
          h("dt", {}, "Model"), h("dd", {}, (state.graph.model_roles || {})[cfg.model_role] || cfg.model_role || "—"),
          h("dt", {}, "Temperatura"), h("dd", {}, cfg.temperature ?? "—"),
          h("dt", {}, "Utworzył(a)"), h("dd", {}, cfg.created_by || "—"),
          h("dt", {}, "Kiedy"), h("dd", {}, cfg.created_at ? fmtDate(cfg.created_at) : "—"),
          cfg.updated_at ? [h("dt", {}, "Zmienił(a)"), h("dd", {}, `${cfg.updated_by}, ${fmtDate(cfg.updated_at)}`)] : null)));
      const actions = [];
      if (n.editable) actions.push(h("button", { class: "btn violet small", onclick: () => openNodeForm(n) }, "✏️ Edytuj węzeł"));
      if (n.can_share) {
        const toggle = h("input", { type: "checkbox", class: "switch", checked: n.global, onchange: () => shareNode(n, toggle.checked, toggle) });
        actions.push(h("label", { class: "share-toggle" }, toggle, h("span", {}, "🌐 Globalny — dostępny dla wszystkich użytkowników")));
      }
      if (actions.length) sections.push(h("div", { class: "insp-section node-actions" }, actions));
    }
    if ((n.tools || []).length) {
      sections.push(h("div", { class: "insp-section" }, h("h4", {}, "Narzędzia planera"),
        n.tools.map((t) => h("div", { class: "tool-card" }, h("code", {}, t.name), h("div", {}, t.description),
          t.when_to_use ? h("div", { class: "small" }, "✅ ", t.when_to_use) : null,
          t.when_not_to_use ? h("div", { class: "small" }, "⛔ ", t.when_not_to_use) : null))));
    }
    const out = state.graph.edges.filter((e) => e.source === n.id);
    const inc = state.graph.edges.filter((e) => e.target === n.id);
    const flow = (e, dir) => h("div", { class: "flow-row" },
      h("span", { class: "arrow" }, dir === "out" ? "→" : "←"), h("span", {}, nodeName(dir === "out" ? e.target : e.source)),
      e.label ? h("span", { class: "chip tag" }, e.label) : null);
    sections.push(h("div", { class: "insp-section" }, h("h4", {}, "Przejścia"),
      inc.map((e) => flow(e, "in")), out.map((e) => flow(e, "out")),
      out.some((e) => e.target === "__next__") ? h("p", { class: "muted", style: { fontSize: "12.5px" } },
        "Po tym węźle agent przechodzi do następnego kroku planu (dowolny węzeł narzędzia) albo kończy.") : null));
    if (n.kind === "custom") {
      sections.push(h("div", { class: "insp-section" }, h("h4", {}, "Prompt systemowy"),
        h("div", { class: "prompt-card" }, h("div", { class: "preview full" }, n.prompt || ""),
          n.editable ? h("button", { class: "btn small", onclick: () => openNodeForm(n) }, "✏️ Zmień prompt i ustawienia") : null)));
    } else if ((n.prompts || []).length) {
      const cards = n.prompts.map((role) => {
        const preview = h("div", { class: "preview" }, "…");
        api(`/prompts/${encodeURIComponent(role)}`).then((p) => { preview.textContent = (p.current || "").slice(0, 400); }).catch(() => {});
        return h("div", { class: "prompt-card" },
          h("div", { style: { display: "flex", alignItems: "center", gap: "8px" } }, h("code", {}, role),
            changed.has(role) ? h("span", { class: "badge mod" }, "zmieniony") : h("span", { class: "badge def" }, "domyślny")),
          preview, h("button", { class: "btn small", onclick: () => openPrompt(role) }, isAdmin() ? "✏️ Edytuj prompt" : "👁 Zobacz prompt"));
      });
      sections.push(h("div", { class: "insp-section" }, h("h4", {}, "Prompty"), cards));
    } else {
      sections.push(h("div", { class: "insp-section" }, h("h4", {}, "Prompty"),
        h("p", { class: "muted" }, "Ten węzeł działa według kodu, bez własnego promptu systemowego.")));
    }
    if (n.kind === "custom" && n.editable) {
      sections.push(h("div", { class: "danger-zone" },
        h("p", {}, n.global ? "Usunięcie węzła globalnego usuwa go z rozmów wszystkich użytkowników." : "Usunięcie węzła usuwa go z Twojego grafu i z katalogu narzędzi planera."),
        h("button", { class: "btn danger", onclick: () => deleteNode(n) }, "🗑️ Usuń węzeł")));
    } else if (n.kind === "custom") {
      sections.push(h("div", { class: "insp-section" }, h("p", { class: "muted" }, "Węzeł globalny udostępniony przez administratora — możesz go używać, ale zmieniać go może tylko administrator.")));
    }
    body.replaceChildren(...sections);
    body.scrollTop = 0;
  }

  async function shareNode(n, value, toggle) {
    try {
      await api(`/nodes/${encodeURIComponent(n.id)}/global`, { method: "PUT", body: { global: value } });
      toast(value ? `Węzeł ${n.id} jest teraz globalny — dostępny w rozmowach wszystkich użytkowników.` : `Węzeł ${n.id} jest znowu prywatny.`, "violet");
      await loadGraph(true);
      selectNode(n.id);
    } catch (err) { toggle.checked = !value; toast(err.message, "error"); }
  }

  async function deleteNode(n) {
    const ok = await confirmDialog({ title: `Usunąć węzeł „${splitLabel(n.label)[1]}”?`, danger: true, confirmLabel: "Usuń węzeł",
      text: "Planer przestanie go używać od następnej prośby. Tej operacji nie można cofnąć (zostaje wpis w dzienniku zmian)." });
    if (!ok) return;
    try {
      await api(`/nodes/${encodeURIComponent(n.id)}`, { method: "DELETE" });
      toast(`Usunięto węzeł ${n.id}.`);
      state.selected = null;
      selectNode(null);
      await loadGraph(true);
    } catch (err) { toast(err.message, "error"); }
  }

  // ------------------------------------------------------------------ add / edit a custom node
  function openNodeForm(existing = null) {
    const editing = Boolean(existing);
    const tool = editing ? (existing.tools || [])[0] || {} : {};
    const cfg = editing ? existing.config || {} : {};
    const f = {};
    const field = (key, label, input, hint, full = false) => {
      f[key] = input;
      return h("div", { class: `field${full ? " full" : ""}` }, h("label", { for: `f-${key}` }, label), input, hint ? h("div", { class: "hint" }, hint) : null);
    };
    const area = (key, attrs, value) => { const t = h("textarea", { id: `f-${key}`, ...attrs }); t.value = value || ""; return t; };
    const preview = h("div", {});
    const errors = h("div", { class: "errors", hidden: true });
    let nameTouched = editing;
    const label = h("input", { id: "f-label", maxlength: 60, placeholder: "🧾 Streszczacz umów", value: editing ? existing.label : "" });
    const name = h("input", { id: "f-name", class: "mono", maxlength: 41, placeholder: "streszczacz_umow", value: editing ? existing.id : "", disabled: editing });
    const temp = h("input", { id: "f-temperature", type: "range", min: 0, max: 1.5, step: 0.05, value: editing ? cfg.temperature ?? 0.3 : 0.3 });
    const tempOut = h("output", {}, "");
    const model = h("select", { id: "f-model_role" },
      Object.entries(state.graph.model_roles || { default: "Model ogólny" }).map(([v, t]) => h("option", { value: v, selected: editing && cfg.model_role === v }, t)));
    const shareBox = isAdmin() && !editing ? h("input", { type: "checkbox", class: "switch", id: "f-global" }) : null;
    const refresh = () => {
      if (!nameTouched) name.value = slugify(label.value);
      tempOut.textContent = Number(temp.value).toFixed(2);
      const global = editing ? existing.global : Boolean(shareBox && shareBox.checked);
      preview.replaceChildren(nodeCard({ id: name.value || "nazwa_wezla", label: label.value || "🟣 Nowy węzeł", kind: "custom", global, mine: true,
        tools: [{ name: name.value || "narzędzie" }], prompts: [] }));
    };
    label.addEventListener("input", refresh);
    name.addEventListener("input", () => { nameTouched = true; refresh(); });
    temp.addEventListener("input", refresh);
    if (shareBox) shareBox.addEventListener("change", refresh);
    const submitLabel = editing ? "💾 Zapisz zmiany" : "✨ Utwórz węzeł";
    const submit = h("button", { class: "btn violet" }, submitLabel);
    const scope = editing
      ? (existing.global ? "Węzeł globalny — zmiana obejmie rozmowy wszystkich użytkowników." : "Węzeł prywatny — działa tylko w Twoich rozmowach.")
      : (isAdmin() ? "Węzeł będzie prywatny, chyba że zaznaczysz „globalny” — wtedy trafi do rozmów wszystkich użytkowników."
                   : "Węzeł będzie prywatny: planer użyje go tylko w Twoich rozmowach.");
    const form = h("form", { onsubmit: async (ev) => {
      ev.preventDefault();
      submit.disabled = true; submit.textContent = editing ? "Zapisuję…" : "Tworzę węzeł…";
      const body = { name: name.value.trim(), label: label.value.trim(), description: f.description.value, when_to_use: f.when_to_use.value,
                     when_not_to_use: f.when_not_to_use.value, example: f.example.value, prompt: f.prompt.value,
                     model_role: model.value, temperature: Number(temp.value) };
      if (shareBox) body.global = shareBox.checked;
      try {
        await api(editing ? `/nodes/${encodeURIComponent(existing.id)}` : "/nodes", { method: editing ? "PUT" : "POST", body });
        closeModal();
        toast(editing ? `Zapisano zmiany węzła ${body.name}.` : `Dodano węzeł ${body.name}. Planer może go używać od następnej prośby.`, "violet");
        if (!editing) state.fresh = body.name;
        await loadGraph(true);
        selectNode(body.name);
      } catch (err) {
        errors.hidden = false; errors.textContent = err.message;
        errors.scrollIntoView({ behavior: "smooth", block: "nearest" });
      } finally { submit.disabled = false; submit.textContent = submitLabel; }
    } },
      h("div", { class: "modal-head" }, h("div", { class: "insp-icon" }, editing ? "✏️" : "✨"),
        h("div", {}, h("h3", {}, editing ? `Edycja węzła ${existing.id}` : "Nowy węzeł własny"),
          h("p", {}, "Węzeł oparty na prompcie: jedno wywołanie modelu z Twoim promptem systemowym, bez kodu i bez narzędzi."))),
      h("div", { class: "modal-body" },
        h("div", { class: "preview-strip" }, preview, h("p", {}, scope, " Planer widzi węzeł jako narzędzie o tej samej nazwie — dobry opis decyduje, kiedy zostanie wybrany.")),
        errors,
        h("div", { class: "form-grid" },
          field("label", "Etykieta", label, "Krótko, z emoji na początku, np. „🧾 Streszczacz umów”."),
          field("name", "Nazwa techniczna", name, editing ? "Nazwy nie można zmienić." : "Małe litery, cyfry i _; to też nazwa narzędzia. Unikalna w całym Hapilocie."),
          field("description", "Opis dla planera", area("description", { maxlength: 600, required: true,
            placeholder: "Co robi węzeł, np. „Streszczenie umowy lub regulaminu w pięciu punktach”" }, editing ? tool.description || existing.description : ""), null, true),
          field("when_to_use", "Kiedy używać", area("when_to_use", { maxlength: 600, placeholder: "Gdy użytkownik prosi o…" }, tool.when_to_use)),
          field("when_not_to_use", "Kiedy nie używać", area("when_not_to_use", { maxlength: 600, placeholder: "Gdy chce pliku PDF (template_pdf)…" }, tool.when_not_to_use)),
          field("example", "Przykład (opcjonalnie)", h("input", { id: "f-example", maxlength: 600, placeholder: "„Streść tę umowę” → streszczacz_umow", value: tool.example || "" }), null, true),
          field("prompt", "Prompt systemowy", area("prompt", { class: "mono" }, editing ? existing.prompt : PROMPT_TEMPLATE),
            "Instrukcja dla modelu. Węzeł dostaje też prośbę użytkownika, zadanie kroku i wyniki wcześniejszych kroków.", true),
          field("model_role", "Model", model, "Który model aktywnego profilu odpowiada."),
          h("div", { class: "field" }, h("label", { for: "f-temperature" }, "Temperatura"), h("div", { class: "range-row" }, temp, tempOut),
            h("div", { class: "hint" }, "0 = przewidywalnie, wyżej = bardziej swobodnie.")),
          shareBox ? h("label", { class: "share-toggle full" }, shareBox,
            h("span", {}, h("b", {}, "🌐 Globalny"), " — węzeł trafi do rozmów wszystkich użytkowników (możesz to później wyłączyć)")) : null)),
      h("div", { class: "modal-foot" }, h("span", { class: "left" }, editing ? "" : (isAdmin() ? "" : "🔒 Tylko dla Ciebie")),
        h("button", { class: "btn", type: "button", onclick: closeModal }, "Anuluj"), submit));
    refresh();
    openModal(form);
    label.focus();
  }

  // ------------------------------------------------------------------ prompts
  async function loadPrompts() {
    state.prompts = await api("/prompts");
    state.promptsLoaded = true;
    renderPromptList();
  }

  function promptOwnerKind(p) {
    const node = state.graph && p.nodes.length ? state.graph.nodes.find((n) => n.id === p.nodes[0]) : null;
    return node ? node.kind : (p.source === "core" ? "core" : "extension");
  }

  function renderPromptList() {
    const q = ($("#prompt-search").value || "").toLowerCase();
    const list = $("#prompt-list");
    const items = state.prompts.filter((p) => !q || p.role.includes(q) || p.source.includes(q) || p.nodes.join(" ").includes(q))
      .map((p) => h("button", { class: `prompt-item${p.role === state.role ? " active" : ""}`, onclick: () => selectRole(p.role) },
        h("div", { class: "row" }, h("span", { class: `dot ${promptOwnerKind(p)}` }), h("code", {}, p.role),
          p.overridden ? h("span", { class: "badge mod", style: { marginLeft: "auto" } }, "zmieniony") : null),
        h("div", { class: "meta" }, h("span", {}, "📦 " + p.source), p.nodes.length ? h("span", {}, "· węzły: " + p.nodes.join(", ")) : null,
          h("span", {}, "· " + plural(p.chars, "znak", "znaki", "znaków")), p.versions ? h("span", {}, "· " + plural(p.versions, "wersja", "wersje", "wersji")) : null)));
    list.replaceChildren(...(items.length ? items : [h("p", { class: "muted", style: { padding: "8px 12px" } }, "Brak promptów pasujących do wyszukiwania.")]));
  }

  async function openPrompt(role) {
    switchTab("prompts");
    if (!state.promptsLoaded) await loadPrompts();
    await selectRole(role);
  }

  async function selectRole(role, force = false) {
    if (!force && state.dirty && role !== state.role) {
      const ok = await confirmDialog({ title: "Porzucić niezapisane zmiany?", text: `Prompt ${state.role} ma niezapisane zmiany.`, confirmLabel: "Porzuć zmiany", danger: true });
      if (!ok) return;
    }
    try {
      state.info = await api(`/prompts/${encodeURIComponent(role)}`);
    } catch (err) { toast(err.message, "error"); return; }
    state.role = role; state.original = state.info.current || ""; state.dirty = false; state.mode = "edit";
    renderPromptList();
    renderEditor();
  }

  function lineDiff(a, b) {
    const x = a.split("\n"), y = b.split("\n"), n = x.length, m = y.length;
    if (n * m > 4e6) return [{ t: "same", s: "(tekst za długi, by pokazać różnice)" }];
    const dp = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1));
    for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) dp[i][j] = x[i] === y[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    const out = []; let i = 0, j = 0;
    while (i < n && j < m) {
      if (x[i] === y[j]) { out.push({ t: "same", s: x[i] }); i++; j++; }
      else if (dp[i + 1][j] >= dp[i][j + 1]) out.push({ t: "del", s: x[i++] });
      else out.push({ t: "add", s: y[j++] });
    }
    while (i < n) out.push({ t: "del", s: x[i++] });
    while (j < m) out.push({ t: "add", s: y[j++] });
    return out;
  }

  function renderEditor() {
    const info = state.info, pane = $("#prompt-editor");
    const text = h("textarea", { class: "prompt-text", spellcheck: "false" });
    text.value = state.original;
    const status = h("div", { class: "editor-status" });
    const canEdit = isAdmin();
    if (!canEdit) text.readOnly = true;
    const save = h("button", { class: "btn primary", disabled: true, onclick: () => savePrompt(text.value) }, "💾 Zapisz nową wersję");
    const restore = h("button", { class: "btn danger-ghost", disabled: !info.overridden, onclick: restorePrompt }, "↩️ Przywróć domyślny");
    const actions = canEdit ? [restore, save] : [h("span", { class: "badge def" }, "👁 tylko do odczytu — zmienia administrator")];
    const updateStatus = () => {
      state.dirty = text.value !== state.original;
      save.disabled = !state.dirty;
      const lines = text.value.split("\n").length;
      status.replaceChildren(...[h("span", {}, plural(text.value.length, "znak", "znaki", "znaków")), h("span", {}, plural(lines, "linia", "linie", "linii")),
        !canEdit ? null : state.dirty ? h("span", { class: "dirty" }, "● niezapisane zmiany") : h("span", {}, "✓ zapisane"),
        canEdit ? h("span", { style: { marginLeft: "auto" } }, "Ctrl+S — zapisz") : null].filter(Boolean));
    };
    text.addEventListener("input", updateStatus);
    text.addEventListener("keydown", (ev) => {
      if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "s") { ev.preventDefault(); if (state.dirty) savePrompt(text.value); }
    });
    const area = h("div", { style: { display: "contents" } });
    const modes = [["edit", "Edycja"], ["diff", "Różnice względem domyślnego"], ["default", "Domyślny"]];
    const switcher = h("div", { class: "mode-switch" });
    const showMode = (mode) => {
      state.mode = mode;
      switcher.replaceChildren(...modes.map(([m, t]) => h("button", { class: m === mode ? "active" : "", onclick: () => showMode(m) }, t)));
      if (mode === "edit") area.replaceChildren(text);
      else if (mode === "default") { const d = h("textarea", { class: "prompt-text", readonly: true }); d.value = info.default || ""; area.replaceChildren(d); }
      else {
        const diff = lineDiff(info.default || "", text.value);
        const changes = diff.filter((d) => d.t !== "same").length;
        area.replaceChildren(h("div", { class: "diff" }, changes ? diff.map((d) => h("div", { class: d.t }, d.s || " "))
          : h("div", { class: "same" }, "Brak różnic — obowiązuje treść domyślna.")));
      }
    };
    showMode("edit");
    const owner = info.nodes.map((id) => h("span", { class: "chip" }, nodeName(id)));
    const history = info.history.length ? info.history.map((v, i) => h("div", { class: `history-item${i === 0 ? " current" : ""}` },
      h("div", { class: "when" }, fmtDate(v.ts), i === 0 ? h("span", { class: "badge now", style: { marginLeft: "6px" } }, "obecna") : null),
      h("div", { class: "who" }, "👤 ", v.user, " · ", v.restored ? "przywrócono domyślny" : plural(v.chars, "znak", "znaki", "znaków")),
      v.text && canEdit ? h("button", { class: "btn small", onclick: () => { text.value = v.text; showMode("edit"); updateStatus(); text.focus(); } }, "Wczytaj do edytora") : null))
      : [h("p", { class: "muted", style: { fontSize: "13px" } }, "Ten prompt nie był jeszcze zmieniany — obowiązuje treść z rozszerzenia.")];
    pane.replaceChildren(
      h("div", { class: "editor-head" },
        h("div", { class: "title" }, h("code", {}, info.role),
          h("div", { class: "meta" }, info.overridden ? h("span", { class: "badge mod" }, "zmieniony przez administratora") : h("span", { class: "badge def" }, "domyślny"),
            h("span", {}, "źródło: 📦 " + info.source), owner.length ? h("span", {}, "używa:") : null, owner)),
        h("div", { class: "actions" }, actions)),
      h("div", { class: "editor-body" },
        h("div", { class: "editor-main" }, switcher, area, status),
        h("div", { class: "history-pane" }, h("h4", {}, "Historia wersji"), history)));
    updateStatus();
  }

  async function savePrompt(text) {
    try {
      state.info = await api(`/prompts/${encodeURIComponent(state.role)}`, { method: "PUT", body: { text } });
      state.original = state.info.current; state.dirty = false;
      toast(`Zapisano nową wersję promptu ${state.role}. Obowiązuje od następnej prośby.`);
      await loadPrompts();
      renderEditor();
      if (state.graph) renderGraph();
    } catch (err) { toast(err.message, "error"); }
  }

  async function restorePrompt() {
    const ok = await confirmDialog({ title: `Przywrócić domyślny prompt ${state.role}?`, confirmLabel: "Przywróć domyślny",
      text: "Obowiązywać zacznie treść z rozszerzenia. Twoje wersje zostają w historii i można je wczytać ponownie." });
    if (!ok) return;
    try {
      state.info = await api(`/prompts/${encodeURIComponent(state.role)}`, { method: "DELETE" });
      state.original = state.info.current; state.dirty = false;
      toast(`Przywrócono domyślny prompt ${state.role}.`);
      await loadPrompts();
      renderEditor();
      if (state.graph) renderGraph();
    } catch (err) { toast(err.message, "error"); }
  }

  // ------------------------------------------------------------------ audit
  async function loadAudit() {
    const box = $("#audit-table");
    try {
      const rows = await api("/audit");
      if (!rows.length) { box.replaceChildren(h("div", { class: "empty-state" }, h("div", { class: "empty-icon" }, "📜"), h("p", {}, "Nikt jeszcze niczego nie zmienił."))); return; }
      box.replaceChildren(h("table", { class: "audit" },
        h("thead", {}, h("tr", {}, ["Kiedy", "Kto", "Zmiana", "Czego dotyczy", "Szczegóły"].map((t) => h("th", {}, t)))),
        h("tbody", {}, rows.map((r) => {
          const [icon, text] = ACTIONS[r.action] || ["•", r.action];
          return h("tr", {}, h("td", {}, fmtDate(r.ts)), h("td", {}, "👤 " + r.user),
            h("td", {}, h("span", { class: `action-pill ${r.action}` }, icon, " ", text)), h("td", {}, h("code", {}, r.target)),
            h("td", { class: "muted" }, r.detail || ""));
        }))));
    } catch (err) { toast(err.message, "error"); }
  }

  // ------------------------------------------------------------------ tabs, theme, start
  function switchTab(tab) {
    for (const b of document.querySelectorAll(".tab")) b.classList.toggle("active", b.dataset.tab === tab);
    for (const v of document.querySelectorAll(".view")) v.classList.toggle("active", v.dataset.view === tab);
    if (location.hash !== "#" + tab) history.replaceState(null, "", "#" + tab);
    if (tab === "prompts" && !state.promptsLoaded) loadPrompts().catch((err) => toast(err.message, "error"));
    if (tab === "audit") loadAudit();
    if (tab === "graph") setTimeout(() => fit(), 0);
  }

  async function loadGraph(keepView = false) {
    state.graph = await api("/graph");
    if (!state.promptsLoaded) await loadPrompts().catch(() => {});
    const view = { ...state.view };
    renderGraph();
    if (keepView) { state.view = view; applyView(); } else fit();
  }

  function setupTheme() {
    $("#theme-btn").onclick = () => {
      const dark = document.documentElement.dataset.theme
        ? document.documentElement.dataset.theme === "dark"
        : matchMedia("(prefers-color-scheme: dark)").matches;
      const next = dark ? "light" : "dark";
      document.documentElement.dataset.theme = next;
      try { localStorage.setItem("hapilot-admin-theme", next); } catch (e) { /* private mode */ }
    };
  }

  async function start() {
    setupTheme();
    setupPanZoom();
    for (const b of document.querySelectorAll(".tab")) b.addEventListener("click", () => switchTab(b.dataset.tab));
    $("#add-node-btn").onclick = () => openNodeForm();
    const legend = $("#legend");
    try { if (localStorage.getItem("hapilot-admin-legend") === "closed") legend.classList.add("collapsed"); } catch (e) { /* private mode */ }
    legend.querySelector(".legend-title").onclick = () => {
      legend.classList.toggle("collapsed");
      try { localStorage.setItem("hapilot-admin-legend", legend.classList.contains("collapsed") ? "closed" : "open"); } catch (e) { /* private mode */ }
    };
    $("#reload-btn").onclick = () => loadGraph(true).then(() => toast("Odświeżono graf."));
    $("#inspector-close").onclick = () => selectNode(null);
    $("#prompt-search").addEventListener("input", renderPromptList);
    $("#modal-backdrop").addEventListener("pointerdown", (ev) => { if (ev.target.id === "modal-backdrop") closeModal(); });
    document.addEventListener("keydown", (ev) => {
      if (ev.key !== "Escape") return;
      if (!$("#modal-backdrop").hidden) closeModal(); else if (state.selected) selectNode(null);
    });
    window.addEventListener("beforeunload", (ev) => { if (state.dirty) { ev.preventDefault(); ev.returnValue = ""; } });
    try {
      const me = await api("/me");
      $("#user-chip").replaceChildren(h("span", { class: "avatar" }, (me.identifier || "?")[0].toUpperCase()), h("span", {}, me.identifier),
        h("span", { class: `role-badge${me.admin ? " admin" : ""}` }, me.admin ? "administrator" : "użytkownik"));
    } catch (e) { /* shown by the API calls below */ }
    try { await loadGraph(); } catch (err) { $("#graph-loading").textContent = "Nie udało się wczytać grafu: " + err.message; }
    const tab = (location.hash || "#graph").slice(1);
    if (["graph", "prompts", "audit"].includes(tab)) switchTab(tab);
  }

  start();
})();
