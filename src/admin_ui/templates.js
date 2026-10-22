/* Hapilot agent panel — the "📄 Szablony" tab: PDF document templates (block builder or LaTeX) with a live preview.
   API: /hapilot/admin/api/templates* (src/ui_admin.py → src/doc_templates.py). Helpers come from admin.js. */
(() => {
  "use strict";

  const A = window.HapilotAdmin;
  const { h, api, toast, confirmDialog, fmtDate } = A;
  const $ = (sel, root = document) => root.querySelector(sel);

  const BLOCKS = {
    title: { icon: "🔠", hint: "Duży, wyśrodkowany tytuł dokumentu", text: 2 },
    heading: { icon: "🔖", hint: "Nagłówek sekcji (poziom 1 lub 2)", text: 1 },
    paragraph: { icon: "¶", hint: "Tekst; pusta linia zaczyna nowy akapit", text: 5 },
    sender: { icon: "🏢", hint: "Dane nadawcy, wyrównane do lewej", text: 4 },
    recipient: { icon: "📮", hint: "Adresat, wyrównany do prawej", text: 4 },
    place_date: { icon: "📅", hint: "Miejscowość i data, do prawej", text: 1 },
    list: { icon: "☰", hint: "Lista z pola: każda linia wartości to jeden punkt" },
    table: { icon: "▦", hint: "Tabela z pola: wiersz w linii, komórki rozdzielone | lub ;" },
    signature: { icon: "✍️", hint: "Linia do podpisu i opis pod nią", text: 2 },
    spacer: { icon: "↕️", hint: "Pionowy odstęp" },
    page_break: { icon: "📃", hint: "Dalsza treść od nowej strony" },
    footer: { icon: "⎯", hint: "Drobny tekst u dołu strony", text: 2 },
  };
  const STARTER = {
    mode: "blocks",
    parameters: {
      MIEJSCE_DATA: { description: "Miejscowość i data", example: "Wrocław, 22 października 2026 r.", required: true, multiline: false },
      ADRESAT: { description: "Adresat (kilka linii)", example: "Firma Testowa Sp. z o.o.\nul. Przykładowa 1\n00-001 Warszawa", required: true, multiline: true },
      TYTUL: { description: "Tytuł dokumentu", example: "Oferta współpracy", required: true, multiline: false },
      TRESC: { description: "Treść dokumentu", example: "Szanowni Państwo,\n\nz przyjemnością przedstawiamy naszą ofertę.", required: true, multiline: true },
      PODPIS: { description: "Podpis (imię, nazwisko, stanowisko)", example: "Anna Przykładowa\nKierownik działu", required: true, multiline: true },
    },
    blocks: [
      { type: "place_date", text: "{{MIEJSCE_DATA}}" },
      { type: "recipient", text: "{{ADRESAT}}" },
      { type: "title", text: "{{TYTUL}}" },
      { type: "paragraph", text: "{{TRESC}}", align: "justify" },
      { type: "signature", text: "{{PODPIS}}" },
    ],
  };
  const st = { list: [], blockTypes: {}, cur: null, original: "", dirty: false, loaded: false, timer: null, seq: 0, latex: "",
               unused: [] };

  // ------------------------------------------------------------------ list
  async function loadList() {
    const data = await api("/templates");
    st.list = data.templates;
    st.blockTypes = data.block_types || {};
    st.loaded = true;
    renderList();
  }

  function kindOf(t) {
    if (t.kind === "builtin") return { cls: "extension", label: "Wbudowany" };
    return t.global ? { cls: "custom", label: "🌐 Globalny" } : { cls: "private", label: "🔒 Prywatny" };
  }

  function renderList() {
    const q = ($("#tpl-search").value || "").toLowerCase();
    const groups = [["Wbudowane", (t) => t.kind === "builtin"], ["Moje", (t) => t.kind === "custom" && t.mine],
                    ["Globalne (od administratora)", (t) => t.kind === "custom" && !t.mine]];
    const out = [];
    for (const [title, test] of groups) {
      const items = st.list.filter(test).filter((t) => !q || (t.id + " " + t.name + " " + t.description).toLowerCase().includes(q));
      if (!items.length) continue;
      out.push(h("div", { class: "tpl-group" }, title));
      for (const t of items) {
        const k = kindOf(t);
        out.push(h("button", { class: `prompt-item${st.cur && st.cur.id === t.id && !st.cur.isNew ? " active" : ""}`, onclick: () => select(t.id) },
          h("div", { class: "row" }, h("span", { class: `dot ${k.cls}` }), h("b", {}, t.name),
            t.overridden ? h("span", { class: "badge mod", style: { marginLeft: "auto" } }, "zmieniony") : null),
          h("div", { class: "meta" }, h("code", {}, t.id), h("span", {}, "· " + A.plural(t.fields, "pole", "pola", "pól")),
            t.kind === "custom" ? h("span", {}, "· " + k.label) : null, t.kind === "custom" && !t.mine ? h("span", {}, "· 👤 " + t.owner) : null)));
      }
    }
    $("#tpl-list").replaceChildren(...(out.length ? out : [h("p", { class: "muted", style: { padding: "8px 12px" } }, "Brak szablonów.")]));
  }

  // ------------------------------------------------------------------ select / new / duplicate
  async function guardDirty() {
    if (!st.dirty) return true;
    return confirmDialog({ title: "Porzucić niezapisane zmiany?", text: `Szablon „${st.cur.name}” ma niezapisane zmiany.`,
                           confirmLabel: "Porzuć zmiany", danger: true });
  }

  async function select(id) {
    if (!(await guardDirty())) return;
    try { st.cur = await api(`/templates/${encodeURIComponent(id)}`); } catch (err) { toast(err.message, "error"); return; }
    st.cur.isNew = false;
    afterLoad();
  }

  async function startNew(from = null) {
    if (!(await guardDirty())) return;
    const base = from ? JSON.parse(JSON.stringify(from)) : JSON.parse(JSON.stringify(STARTER));
    st.cur = { ...base, id: from ? `${from.id}_kopia`.slice(0, 41) : "", name: from ? `${from.name} (kopia)` : "",
               description: from ? from.description : "", kind: "custom", isNew: true, editable: true, mine: true,
               global: false, history: [] };
    if (from && from.kind === "builtin") st.cur.mode = "latex";
    afterLoad();
    setTimeout(() => { const n = $("#tpl-name"); if (n) n.focus(); }, 50);
  }

  function afterLoad() {
    st.original = JSON.stringify(payload());
    st.dirty = false; st.unused = []; st.latex = st.cur.latex || "";
    renderList();
    renderEditor();
    preview(0);
  }

  function payload() {
    const c = st.cur;
    const body = { id: c.id, name: c.name, description: c.description, mode: c.mode, parameters: c.parameters };
    if (c.mode === "blocks") body.blocks = c.blocks; else body.latex = c.latex;
    return body;
  }

  function changed(previewDelay = 1100) {
    st.dirty = JSON.stringify(payload()) !== st.original;
    const save = $("#tpl-save");
    if (save) save.disabled = !st.dirty && !st.cur.isNew;
    const flag = $("#tpl-dirty");
    if (flag) flag.textContent = st.dirty ? "● niezapisane zmiany" : "";
    refreshUsage();
    preview(previewDelay);
  }

  // ------------------------------------------------------------------ editor
  function textOfTemplate() {
    const c = st.cur;
    return c.mode === "blocks" ? c.blocks.map((b) => `${b.text || ""} {{${b.field || ""}}}`).join("\n") : c.latex || "";
  }

  function usage(name) {
    const re = new RegExp(`\\{\\{${name}(\\|[a-z]+(:\\d+)?)?\\}\\}`, "g");
    return (textOfTemplate().match(re) || []).length;
  }

  function refreshUsage() {
    for (const el of document.querySelectorAll("[data-usage]")) {
      const n = usage(el.dataset.usage);
      el.textContent = n ? `użyte ${n}×` : "nieużywane";
      el.className = n ? "chip" : "chip mod";
    }
  }

  function fieldMenu(onPick) {
    const names = Object.keys(st.cur.parameters);
    const sel = h("select", { class: "insert-field", title: "Wstaw pole w miejscu kursora", disabled: !st.cur.editable },
      h("option", { value: "" }, "＋ pole…"), names.map((n) => h("option", { value: n }, n)));
    sel.addEventListener("change", () => { if (sel.value) onPick(sel.value); sel.value = ""; });
    return sel;
  }

  function insertAt(textarea, snippet) {
    const { selectionStart: a, selectionEnd: b, value } = textarea;
    textarea.value = value.slice(0, a) + snippet + value.slice(b);
    textarea.selectionStart = textarea.selectionEnd = a + snippet.length;
    textarea.dispatchEvent(new Event("input"));
    textarea.focus();
  }

  function renderEditor() {
    const c = st.cur, ro = !c.editable;
    const k = c.kind === "builtin" ? { cls: "extension", label: "Wbudowany" } : kindOf(c);
    const name = h("input", { id: "tpl-name", class: "tpl-title-input", value: c.name, placeholder: "Nazwa szablonu, np. Oferta handlowa",
                              maxlength: 80, disabled: ro });
    name.addEventListener("input", () => {
      const auto = c.isNew && (!c.id || c.id === A.slugify(c._lastName || ""));
      c._lastName = name.value; c.name = name.value;
      if (auto) { c.id = A.slugify(name.value); const idEl = $("#tpl-id"); if (idEl) idEl.value = c.id; }
      changed();
    });
    const id = h("input", { id: "tpl-id", class: "mono", value: c.id, disabled: !c.isNew, maxlength: 41, placeholder: "oferta_handlowa" });
    id.addEventListener("input", () => { c.id = id.value.trim(); changed(); });
    const desc = h("textarea", { class: "tpl-desc", maxlength: 600, disabled: ro,
                                 placeholder: "Kiedy model ma wybrać ten szablon, np. „Oferta cenowa dla klienta z tabelą pozycji i warunkami”" });
    desc.value = c.description || "";
    desc.addEventListener("input", () => { c.description = desc.value; changed(5000); });

    const actions = [];
    if (c.editable) actions.push(h("button", { class: "btn primary", id: "tpl-save", disabled: !c.isNew, onclick: save }, c.isNew ? "✨ Utwórz szablon" : "💾 Zapisz"));
    if (!c.isNew) actions.push(h("button", { class: "btn", onclick: () => startNew(c) }, "⧉ Duplikuj"));
    if (c.kind === "builtin" && c.editable && c.overridden) actions.push(h("button", { class: "btn danger-ghost", onclick: restore }, "↩️ Przywróć domyślny"));
    if (c.kind === "custom" && !c.isNew && c.editable) actions.push(h("button", { class: "btn danger-ghost", onclick: remove }, "🗑️ Usuń"));

    const chips = [h("span", { class: `chip kind tpl-kind ${k.cls}` }, c.isNew ? (A.isAdmin() ? "Nowy szablon" : "🔒 Prywatny") : k.label)];
    if (c.kind === "custom" && !c.isNew && !c.mine) chips.push(h("span", { class: "chip" }, "👤 " + c.owner));
    if (c.overridden) chips.push(h("span", { class: "badge mod" }, "zmieniony przez administratora"));
    let share = null;
    if ((c.can_share && !c.isNew) || (c.isNew && A.isAdmin())) {
      const toggle = h("input", { type: "checkbox", class: "switch", checked: Boolean(c.global) });
      toggle.addEventListener("change", () => (c.isNew ? (c.global = toggle.checked) : setGlobal(toggle)));
      share = h("label", { class: "share-toggle" }, toggle, h("span", {}, "🌐 Globalny — dla wszystkich użytkowników"));
    }
    const banner = ro ? h("div", { class: "tpl-banner" }, c.kind === "builtin"
      ? "👁 Wbudowany szablon — możesz go obejrzeć i zduplikować; zmienia go administrator."
      : "👁 Szablon globalny od administratora — możesz go używać i zduplikować.",
      h("button", { class: "btn small", onclick: () => startNew(c) }, "⧉ Duplikuj, aby zmienić")) : null;

    const modeSwitch = h("div", { class: "mode-switch" },
      [["blocks", "🧱 Bloki"], ["latex", "{ } LaTeX"]].map(([m, t]) => h("button", {
        class: c.mode === m ? "active" : "", disabled: ro || c.kind === "builtin" || (m === "blocks" && c.mode === "latex"),
        title: m === "blocks" && c.mode === "latex" ? "Z trybu LaTeX nie można wrócić do bloków" : "",
        onclick: () => switchMode(m) }, t)));

    const content = c.mode === "blocks" ? blocksEditor(ro) : latexEditor(ro);
    $("#tpl-editor").replaceChildren(h("div", { class: "tpl-editor-inner" },
      banner,
      h("div", { class: "tpl-head" },
        h("div", { class: "tpl-head-main" }, name,
          h("div", { class: "tpl-meta" }, chips, h("span", { class: "muted" }, "identyfikator:"), id, h("span", { class: "dirty", id: "tpl-dirty" }))),
        h("div", { class: "tpl-actions" }, actions)),
      share,
      h("div", { class: "field full" }, h("label", {}, "Opis dla modelu"), desc,
        h("div", { class: "hint" }, "Na tej podstawie Hapilot wybiera szablon. Użytkownik może też wskazać go po nazwie: „przygotuj dokument według szablonu ", c.name || "…", "”.")),
      h("div", { class: "tpl-section-head" }, h("h3", {}, "Treść dokumentu"), modeSwitch),
      content,
      h("div", { class: "tpl-section-head" }, h("h3", {}, "Pola"), c.editable ? h("button", { class: "btn small", onclick: addField }, "＋ Dodaj pole") : null),
      h("p", { class: "muted small-note" }, "Pola wypełnia model (i użytkownik w formularzu przed wygenerowaniem PDF). Przykład trafia do podglądu i podpowiada modelowi format."),
      fieldsTable(ro),
      c.kind === "builtin" && c.history && c.history.length ? h("div", {},
        h("div", { class: "tpl-section-head" }, h("h3", {}, "Historia zmian")),
        c.history.map((v) => h("div", { class: "history-item" }, h("span", { class: "when" }, fmtDate(v.ts)), " · 👤 ", v.user,
          v.restored ? " · przywrócono domyślny" : ""))) : null,
      c.config && c.config.created_by ? h("p", { class: "muted small-note" }, `Utworzył(a): ${c.config.created_by}, ${fmtDate(c.config.created_at)}`,
        c.config.updated_by ? ` · zmienił(a): ${c.config.updated_by}, ${fmtDate(c.config.updated_at)}` : "") : null));
    refreshUsage();
  }

  function blocksEditor(ro) {
    const c = st.cur;
    const cards = c.blocks.map((b, i) => {
      const meta = BLOCKS[b.type] || { icon: "?" };
      const controls = [];
      if (meta.text) {
        const ta = h("textarea", { rows: meta.text, disabled: ro, placeholder: meta.hint });
        ta.value = b.text || "";
        ta.addEventListener("input", () => { b.text = ta.value; changed(); });
        controls.push(h("div", { class: "block-text" }, ta, ro ? null : fieldMenu((f) => insertAt(ta, `{{${f}}}`))));
      }
      const select = (key, options, label) => {
        const s = h("select", { disabled: ro }, options.map(([v, t]) => h("option", { value: v, selected: (b[key] || options[0][0]) === v }, t)));
        s.addEventListener("change", () => { b[key] = s.value; changed(400); });
        return h("label", { class: "block-opt" }, label, s);
      };
      if (b.type === "paragraph") controls.push(select("align", [["justify", "Wyjustowany"], ["left", "Do lewej"], ["center", "Wyśrodkowany"], ["right", "Do prawej"]], "Wyrównanie"));
      if (b.type === "heading") controls.push(select("level", [[1, "Poziom 1"], [2, "Poziom 2"]].map(([v, t]) => [String(v), t]), "Poziom"));
      if (b.type === "list" || b.type === "table") {
        const names = Object.keys(c.parameters);
        controls.push(select("field", [["", "— wybierz pole —"], ...names.map((n) => [n, n])], "Pole"));
      }
      if (b.type === "list") controls.push(select("style", [["bullets", "Punkty •"], ["numbers", "Numeracja 1. 2. 3."]], "Styl"));
      if (b.type === "table") {
        const cols = h("input", { value: b.columns || "", disabled: ro, placeholder: "Lp.;Usługa;Cena" });
        cols.addEventListener("input", () => { b.columns = cols.value; changed(); });
        controls.push(h("label", { class: "block-opt wide" }, "Kolumny (oddziel ;)", cols));
      }
      if (b.type === "spacer") controls.push(select("size", [["medium", "Średni"], ["small", "Mały"], ["large", "Duży"]], "Wielkość"));
      const move = (d) => { const j = i + d; if (j < 0 || j >= c.blocks.length) return; [c.blocks[i], c.blocks[j]] = [c.blocks[j], c.blocks[i]]; renderEditor(); changed(400); };
      return h("div", { class: "block-card" },
        h("div", { class: "block-head" }, h("span", { class: "block-icon" }, meta.icon), h("b", {}, st.blockTypes[b.type] || b.type),
          h("span", { class: "muted block-hint" }, meta.hint),
          ro ? null : h("span", { class: "block-tools" },
            h("button", { class: "icon-btn mini", title: "W górę", disabled: i === 0, onclick: () => move(-1) }, "↑"),
            h("button", { class: "icon-btn mini", title: "W dół", disabled: i === c.blocks.length - 1, onclick: () => move(1) }, "↓"),
            h("button", { class: "icon-btn mini", title: "Usuń blok", onclick: () => { c.blocks.splice(i, 1); renderEditor(); changed(400); } }, "✕"))),
        controls.length ? h("div", { class: "block-body" }, controls) : null);
    });
    const palette = ro ? null : h("div", { class: "block-palette" }, h("span", { class: "muted" }, "Dodaj blok:"),
      Object.entries(BLOCKS).map(([type, meta]) => h("button", { class: "chip-btn", title: meta.hint, onclick: () => {
        const block = { type };
        if (type === "list" || type === "table") block.field = Object.keys(c.parameters).find((n) => c.parameters[n].multiline) || "";
        if (type === "table") block.columns = "Lp.;Nazwa;Wartość";
        c.blocks.push(block); renderEditor(); changed(600);
        const cards = document.querySelectorAll(".block-card"); if (cards.length) cards[cards.length - 1].scrollIntoView({ behavior: "smooth", block: "center" });
      } }, meta.icon, " ", st.blockTypes[type] || type)));
    return h("div", { class: "blocks" }, cards.length ? cards : h("p", { class: "muted" }, "Brak bloków — dodaj pierwszy poniżej."), palette);
  }

  function latexEditor(ro) {
    const c = st.cur;
    const ta = h("textarea", { class: "prompt-text tpl-latex", spellcheck: "false", disabled: ro });
    ta.value = c.latex || "";
    ta.addEventListener("input", () => { c.latex = ta.value; changed(1400); });
    ta.addEventListener("keydown", (ev) => {
      if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "s") { ev.preventDefault(); if (c.editable) save(); }
    });
    return h("div", { class: "latex-wrap" },
      h("div", { class: "latex-help" }, ro ? null : fieldMenu((f) => insertAt(ta, `{{${f}}}`)),
        h("span", {}, "Pola: ", h("code", {}, "{{POLE}}"), " · lista: ", h("code", {}, "{{POLE|lista}}"), " (w itemize) · tabela: ",
          h("code", {}, "{{POLE|tabela:3}}"), " · zachowaj linie: ", h("code", {}, "{{POLE|linie}}"), " · akapity: ", h("code", {}, "{{POLE|akapity}}"))),
      ta);
  }

  function fieldsTable(ro) {
    const c = st.cur;
    const rows = Object.entries(c.parameters).map(([key, f]) => {
      const name = h("input", { class: "mono", value: key, disabled: ro || c.kind === "builtin", maxlength: 41 });
      name.addEventListener("change", () => renameField(key, name.value));
      const label = h("input", { value: f.description || "", disabled: ro, placeholder: "Etykieta w formularzu" });
      label.addEventListener("input", () => { f.description = label.value; changed(5000); });
      const example = h(f.multiline ? "textarea" : "input", { disabled: ro, placeholder: "Przykładowa wartość", rows: 2 });
      example.value = f.example || "";
      example.addEventListener("input", () => { f.example = example.value; changed(); });
      const req = h("input", { type: "checkbox", checked: f.required !== false, disabled: ro, title: "Wymagane" });
      req.addEventListener("change", () => { f.required = req.checked; changed(5000); });
      const multi = h("input", { type: "checkbox", checked: Boolean(f.multiline), disabled: ro, title: "Wiele linii" });
      multi.addEventListener("change", () => { f.multiline = multi.checked; renderEditor(); changed(5000); });
      return h("tr", {},
        h("td", {}, name, h("div", {}, h("span", { "data-usage": key, class: "chip" }))), h("td", {}, label), h("td", {}, example),
        h("td", { class: "center" }, req), h("td", { class: "center" }, multi),
        h("td", {}, ro || c.kind === "builtin" ? null : h("button", { class: "icon-btn mini", title: "Usuń pole", onclick: () => { delete c.parameters[key]; renderEditor(); changed(400); } }, "✕")));
    });
    return h("table", { class: "fields-table" },
      h("thead", {}, h("tr", {}, ["Pole", "Etykieta", "Przykład", "Wymagane", "Wiele linii", ""].map((t) => h("th", {}, t)))),
      h("tbody", {}, rows));
  }

  function addField() {
    let n = 1, key = "NOWE_POLE";
    while (st.cur.parameters[key]) key = `NOWE_POLE_${++n}`;
    st.cur.parameters[key] = { description: "Nowe pole", example: "", required: true, multiline: false };
    renderEditor();
    changed(5000);
  }

  function renameField(oldKey, value) {
    const c = st.cur, key = value.trim().toUpperCase().replace(/[^A-Z0-9_]/g, "_");
    if (!key || key === oldKey) { renderEditor(); return; }
    if (c.parameters[key]) { toast(`Pole ${key} już istnieje.`, "error"); renderEditor(); return; }
    c.parameters = Object.fromEntries(Object.entries(c.parameters).map(([k, v]) => [k === oldKey ? key : k, v]));
    const re = new RegExp(`\\{\\{${oldKey}(\\||\\}\\})`, "g");
    for (const b of c.blocks || []) {
      if (b.text) b.text = b.text.replace(re, `{{${key}$1`);
      if (b.field === oldKey) b.field = key;
    }
    if (c.latex) c.latex = c.latex.replace(re, `{{${key}$1`);
    renderEditor();
    changed(400);
  }

  async function switchMode(mode) {
    const c = st.cur;
    if (mode === c.mode || mode !== "latex") return;
    const ok = await confirmDialog({ title: "Przejść do trybu LaTeX?", confirmLabel: "Przejdź do LaTeX",
      text: "Szablon zamienię na kod LaTeX wygenerowany z bloków. Dalej edytujesz go jak zwykły LaTeX — powrotu do bloków nie ma." });
    if (!ok) return;
    try {
      const result = await api("/templates/preview", { method: "POST", body: payload() });
      c.latex = result.latex; c.mode = "latex"; c.blocks = [];
      renderEditor();
      changed(0);
    } catch (err) { toast(err.message, "error"); }
  }

  // ------------------------------------------------------------------ preview
  function preview(delay) {
    clearTimeout(st.timer);
    st.timer = setTimeout(runPreview, delay);
  }

  async function runPreview() {
    if (!st.cur) return;
    const seq = ++st.seq;
    const status = $("#tpl-status"), pages = $("#tpl-pages");
    status.className = "tpl-status busy"; status.textContent = "Kompiluję…";
    pages.classList.add("stale");
    try {
      const r = await api("/templates/preview", { method: "POST", body: { ...payload(), id: st.cur.id || "podglad_szablonu" } });
      if (seq !== st.seq) return;
      pages.classList.remove("stale");
      st.unused = r.unused_fields || [];
      if (!r.ok) {
        status.className = "tpl-status error"; status.textContent = "Błąd kompilacji";
        pages.replaceChildren(h("div", { class: "errors" }, "Szablon się nie kompiluje — ten dokument nie powstanie. Fragment dziennika LaTeX:"),
          h("pre", { class: "latex-log" }, r.log || r.error || ""));
        return;
      }
      const secs = (r.ms / 1000).toLocaleString("pl-PL", { maximumFractionDigits: 1 });
      status.className = "tpl-status ok";
      status.textContent = `✓ ${secs} s · ${A.plural(r.page_count || r.pages.length, "strona", "strony", "stron")}`;
      const notes = [];
      if (st.unused.length) notes.push(h("div", { class: "tpl-note" }, "⚠️ Pola nieużyte w treści: ", st.unused.join(", ")));
      if (r.render_error) notes.push(h("div", { class: "tpl-note" }, "PDF powstał, ale nie udało się go narysować: ", r.render_error));
      pages.replaceChildren(...notes, ...r.pages.map((p) => h("figure", { class: "pdf-page", style: { aspectRatio: `1 / ${p.ratio}` } },
        h("img", { src: p.src, alt: `Strona ${p.n}` }), h("figcaption", {}, `${p.n}`))),
        (r.page_count || 0) > r.pages.length ? h("p", { class: "muted" }, `… i ${r.page_count - r.pages.length} kolejnych stron`) : null);
    } catch (err) {
      if (seq !== st.seq) return;
      pages.classList.remove("stale");
      status.className = "tpl-status warn"; status.textContent = "Do uzupełnienia";
      pages.replaceChildren(h("div", { class: "errors warn" }, err.message));
    }
  }

  // ------------------------------------------------------------------ save, share, delete, restore
  async function save() {
    const c = st.cur;
    const btn = $("#tpl-save");
    if (btn) { btn.disabled = true; btn.textContent = "Kompiluję i zapisuję…"; }
    try {
      const body = payload();
      if (c.isNew && c.global) body.global = true;
      const saved = c.isNew ? await api("/templates", { method: "POST", body })
                            : await api(`/templates/${encodeURIComponent(c.id)}`, { method: "PUT", body });
      toast(c.isNew ? `Utworzono szablon „${saved.name}”. Hapilot użyje go w ${saved.global ? "rozmowach wszystkich użytkowników" : "Twoich rozmowach"}.`
                    : `Zapisano szablon „${saved.name}”.`, "violet");
      st.cur = { ...saved, isNew: false };
      await loadList();
      afterLoad();
    } catch (err) {
      toast(err.message.split("\n")[0], "error");
      if (btn) { btn.disabled = false; btn.textContent = c.isNew ? "✨ Utwórz szablon" : "💾 Zapisz"; }
      if (err.message.includes("\n")) $("#tpl-pages").replaceChildren(h("div", { class: "errors" }, err.message));
    }
  }

  async function setGlobal(toggle) {
    try {
      await api(`/templates/${encodeURIComponent(st.cur.id)}/global`, { method: "PUT", body: { global: toggle.checked } });
      toast(toggle.checked ? `Szablon „${st.cur.name}” jest teraz dostępny dla wszystkich użytkowników.` : `Szablon „${st.cur.name}” jest znowu prywatny.`, "violet");
      st.cur.global = toggle.checked;
      await loadList();
      renderEditor();
    } catch (err) { toggle.checked = !toggle.checked; toast(err.message, "error"); }
  }

  async function remove() {
    const c = st.cur;
    const ok = await confirmDialog({ title: `Usunąć szablon „${c.name}”?`, danger: true, confirmLabel: "Usuń szablon",
      text: c.global ? "To szablon globalny — zniknie z rozmów wszystkich użytkowników. Utworzone dokumenty zostają." : "Szablon zniknie z Twoich rozmów. Utworzone dokumenty zostają." });
    if (!ok) return;
    try {
      await api(`/templates/${encodeURIComponent(c.id)}`, { method: "DELETE" });
      toast(`Usunięto szablon „${c.name}”.`);
      st.cur = null; st.dirty = false;
      await loadList();
      $("#tpl-editor").replaceChildren(h("div", { class: "empty-state" }, h("div", { class: "empty-icon" }, "📄"), h("p", {}, "Wybierz szablon z listy albo utwórz nowy.")));
      $("#tpl-pages").replaceChildren();
      $("#tpl-status").textContent = "";
    } catch (err) { toast(err.message, "error"); }
  }

  async function restore() {
    const ok = await confirmDialog({ title: `Przywrócić domyślny szablon „${st.cur.name}”?`, confirmLabel: "Przywróć domyślny",
      text: "Wszyscy użytkownicy dostaną z powrotem wersję z repozytorium. Twoje zmiany zostają w historii (dzienniku)." });
    if (!ok) return;
    try {
      st.cur = { ...(await api(`/templates/${encodeURIComponent(st.cur.id)}`, { method: "DELETE" })), isNew: false };
      toast("Przywrócono domyślną wersję szablonu.");
      await loadList();
      afterLoad();
    } catch (err) { toast(err.message, "error"); }
  }

  // ------------------------------------------------------------------ my signature
  async function openSignature() {
    let sig;
    try { sig = await api("/signature"); } catch (err) { toast(err.message, "error"); return; }
    const errors = h("div", { class: "errors", hidden: true });
    const input = h("input", { type: "file", accept: "image/png,image/jpeg", hidden: true });
    const upload = (file) => {
      errors.hidden = true;
      if (!file) return;
      if (!["image/png", "image/jpeg"].includes(file.type)) { errors.hidden = false; errors.textContent = "Wybierz plik PNG albo JPG."; return; }
      if (file.size > 2 * 1024 * 1024) { errors.hidden = false; errors.textContent = "Plik jest za duży (najwyżej 2 MB)."; return; }
      const reader = new FileReader();
      reader.onload = async () => {
        try {
          await api("/signature", { method: "PUT", body: { data: reader.result } });
          toast("Zapisano skan podpisu. Wybierzesz go w formularzu dokumentu („Podpis”).", "violet");
          A.closeModal(); openSignature();
        } catch (err) { errors.hidden = false; errors.textContent = err.message; }
      };
      reader.readAsDataURL(file);
    };
    input.addEventListener("change", () => upload(input.files[0]));
    const drop = h("div", { class: "sig-drop", tabindex: "0", onclick: () => input.click(),
                            onkeydown: (ev) => { if (ev.key === "Enter" || ev.key === " ") input.click(); } },
      h("div", { class: "empty-icon" }, "✍️"), h("b", {}, sig.present ? "Przeciągnij nowy plik albo kliknij, aby zastąpić" : "Przeciągnij plik PNG lub JPG albo kliknij"),
      h("span", { class: "muted" }, "najwyżej 2 MB"));
    for (const type of ["dragenter", "dragover"]) drop.addEventListener(type, (ev) => { ev.preventDefault(); drop.classList.add("over"); });
    for (const type of ["dragleave", "drop"]) drop.addEventListener(type, () => drop.classList.remove("over"));
    drop.addEventListener("drop", (ev) => { ev.preventDefault(); upload(ev.dataTransfer.files[0]); });
    const current = sig.present ? h("div", { class: "sig-current" },
      h("div", { class: "sig-paper" }, h("img", { src: sig.src, alt: "Twój podpis" }), h("div", { class: "sig-line" }), h("div", { class: "sig-name" }, "tak podpis wypadnie nad linią")),
      h("div", { class: "sig-meta" }, h("span", {}, `${String(sig.format).toUpperCase()} · ${sig.width}×${sig.height} px`),
        h("span", { class: "muted" }, "wgrano " + fmtDate(sig.uploaded_at)),
        h("button", { class: "btn danger-ghost small", onclick: async () => {
          const ok = await confirmDialog({ title: "Usunąć skan podpisu?", danger: true, confirmLabel: "Usuń",
            text: "Nowe dokumenty dostaną puste miejsce na podpis. Już utworzone dokumenty się nie zmienią." });
          if (!ok) { openSignature(); return; }
          try { await api("/signature", { method: "DELETE" }); toast("Usunięto skan podpisu."); openSignature(); } catch (err) { toast(err.message, "error"); }
        } }, "🗑️ Usuń skan"))) : null;
    A.openModal(h("div", {},
      h("div", { class: "modal-head" }, h("div", { class: "insp-icon" }, "✍️"),
        h("div", {}, h("h3", {}, "Mój podpis"), h("p", {}, "Skan widzisz tylko Ty — nie widzą go inni użytkownicy ani administrator. Trafia do dokumentu tylko wtedy, gdy wybierzesz go w formularzu („Podpis → Skan mojego podpisu”)."))),
      h("div", { class: "modal-body" }, errors, current, drop, input,
        h("ul", { class: "sig-tips" },
          h("li", {}, "Najlepiej PNG z przezroczystym albo białym tłem."),
          h("li", {}, "Przytnij obraz tuż przy podpisie — Hapilot dopasuje go do ok. 5,6 × 1,5 cm nad linią."),
          h("li", {}, "Ciemny tusz (czarny, granatowy) wygląda najlepiej po wydruku."))),
      h("div", { class: "modal-foot" }, h("button", { class: "btn", onclick: A.closeModal }, "Zamknij"))));
  }

  // ------------------------------------------------------------------ start
  $("#tpl-new").onclick = () => startNew();
  $("#tpl-signature").onclick = openSignature;
  $("#tpl-search").addEventListener("input", renderList);
  $("#tpl-refresh").onclick = () => preview(0);

  window.HapilotTemplates = {
    open: () => { if (!st.loaded) loadList().catch((err) => toast(err.message, "error")); },
    signature: openSignature,
    dirty: () => st.dirty,
  };
})();
