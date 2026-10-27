/* Hapilot agent panel — the "📚 Baza wiedzy" tab (administrators): the documents the assistant answers from, with
   upload (several files, drag and drop), replace, delete to a trash, restore and a preview of the indexed text.
   API: /hapilot/admin/api/kb… (src/ui_admin.py → src/knowledge_admin.py). Files are sent as the request body.
   Helpers come from admin.js. */
(() => {
  "use strict";

  const A = window.HapilotAdmin;
  const { h, api, toast, confirmDialog, openModal, closeModal, fmtDate, plural } = A;
  const API = "/hapilot/admin/api";
  const $ = (sel, root = document) => root.querySelector(sel);
  const ICONS = { ".pdf": "📕", ".docx": "📘", ".xlsx": "📗", ".csv": "📊", ".eml": "✉️", ".txt": "📄", ".md": "📝" };
  const INLINE = [".pdf", ".txt", ".md"];
  const state = { info: null, uploads: [], sending: false, timer: null, sort: { key: "name", dir: 1 }, filter: "all",
                  query: "", awaitScan: undefined, built: false, nextId: 1 };

  const enc = encodeURIComponent;
  const viewActive = () => $('[data-view="knowledge"]').classList.contains("active");
  const fmtSize = (n) => n < 1024 ? `${n} B` : n < 1048576 ? `${Math.max(1, Math.round(n / 1024))} kB`
    : `${(n / 1048576).toLocaleString("pl-PL", { maximumFractionDigits: 1 })} MB`;
  const ext = (name) => { const m = /\.[^.]+$/.exec(name || ""); return m ? m[0].toLowerCase() : ""; };

  function ago(iso) {
    if (!iso) return "";
    const min = Math.floor((Date.now() - new Date(iso).getTime()) / 60000);
    return min < 1 ? "przed chwilą" : min < 60 ? `${min} min temu` : fmtDate(iso);
  }

  // ------------------------------------------------------------------ sending files (progress needs XMLHttpRequest)
  function send(method, path, file, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open(method, API + path);
      xhr.setRequestHeader("X-Hapilot-Admin", "1");
      xhr.setRequestHeader("Content-Type", "application/octet-stream");
      xhr.upload.onprogress = (ev) => { if (ev.lengthComputable && onProgress) onProgress(ev.loaded / ev.total); };
      xhr.onload = () => {
        let data = null;
        try { data = JSON.parse(xhr.responseText); } catch (e) { /* empty body */ }
        if (xhr.status === 401) { location.href = "/login"; return; }
        if (xhr.status >= 200 && xhr.status < 300) resolve(data);
        else reject(Object.assign(new Error((data && data.error) || `Błąd ${xhr.status}`), { status: xhr.status }));
      };
      xhr.onerror = () => reject(new Error("Połączenie z serwerem zostało przerwane."));
      xhr.send(file);
    });
  }

  function checkFile(file) {
    const limits = state.info.limits;
    if (!limits.types.includes(ext(file.name))) return `nieobsługiwany typ — dozwolone: ${limits.types.join(", ")}`;
    if (file.size > limits.max_bytes) return `za duży (${fmtSize(file.size)}; najwyżej ${limits.max_mb} MB)`;
    if (!file.size) return "plik jest pusty";
    return null;
  }

  function addFiles(list) {
    if (!state.info.writable) { toast("Ten folder dokumentów jest tylko do odczytu (przykładowe dokumenty Hapilota).", "error"); return; }
    for (const file of list) {
      const problem = checkFile(file);
      state.uploads.push({ id: state.nextId++, file, name: file.name, size: file.size, progress: 0,
                           phase: problem ? "error" : "wait", message: problem || "" });
    }
    renderUploads();
    if (!state.sending) runUploads();
  }

  async function runUploads() {
    state.sending = true;
    let item;
    while ((item = state.uploads.find((u) => u.phase === "wait"))) {
      item.phase = "send";
      renderUploads();
      const progress = (p) => { item.progress = p; renderUploads(); };
      try {
        item.source = (await send("POST", `/kb/upload?name=${enc(item.name)}`, item.file, progress)).source;
        item.phase = "queued";
      } catch (err) {
        if (err.status === 409) {
          const ok = await confirmDialog({ title: "Zastąpić dokument?", confirmLabel: "Zastąp nową wersją",
            text: `${err.message} Obecna wersja trafi do kosza na ${state.info.limits.trash_days} dni — można ją przywrócić.` });
          if (ok) {
            try {
              item.source = (await send("POST", `/kb/upload?name=${enc(item.name)}&replace=1`, item.file, progress)).source;
              item.phase = "queued";
            } catch (again) { item.phase = "error"; item.message = again.message; }
          } else { item.phase = "skipped"; item.message = "pominięto — dokument o tej nazwie już jest"; }
        } else { item.phase = "error"; item.message = err.message; }
      }
      item.file = null;
      renderUploads();
      await refresh();
    }
    state.sending = false;
    schedulePoll();
  }

  // ------------------------------------------------------------------ data
  async function refresh() {
    try {
      state.info = await api("/kb");
    } catch (err) { toast(err.message, "error"); return; }
    for (const u of state.uploads.filter((x) => x.phase === "queued")) {
      const f = state.info.files.find((x) => x.source === u.source);
      if (f && f.status === "indexed") { u.phase = "done"; u.message = `zaindeksowany: ${plural(f.chunks || 0, "fragment", "fragmenty", "fragmentów")}` + (f.ocr_pages ? `, OCR ${f.ocr_pages} str.` : ""); }
      else if (f && f.status === "error") { u.phase = "error"; u.message = f.error || "nie udało się odczytać tekstu"; }
    }
    const scan = state.info.last_scan;
    if (state.awaitScan !== undefined && scan && scan.at !== state.awaitScan && !state.info.queue.rescan) {
      state.awaitScan = undefined;
      toast(`Folder sprawdzony: nowe ${scan.added}, zmienione ${scan.changed}, usunięte ${scan.removed}.`, "success");
    }
    render();
    schedulePoll();
  }

  function busy() {
    const q = state.info && state.info.queue;
    return Boolean(q && (q.waiting || q.running || q.rescan)) || state.uploads.some((u) => ["wait", "send", "queued"].includes(u.phase));
  }

  function schedulePoll() {
    clearTimeout(state.timer);
    if (!viewActive()) return;
    state.timer = setTimeout(() => { if (viewActive()) refresh(); }, busy() ? 2000 : 30000);
  }

  // ------------------------------------------------------------------ actions
  async function rescanNow() {
    state.awaitScan = state.info.last_scan ? state.info.last_scan.at : null;
    try { await api("/kb/rescan", { method: "POST" }); } catch (err) { toast(err.message, "error"); return; }
    toast("Sprawdzam folder dokumentów…");
    refresh();
  }

  function replace(f) {
    const input = h("input", { type: "file", accept: f.ext, hidden: true, onchange: async (ev) => {
      const file = ev.target.files[0];
      input.remove();
      if (!file) return;
      if (ext(file.name) !== f.ext) { toast(`Nowa wersja musi być plikiem ${f.ext} — tego samego typu co obecna.`, "error"); return; }
      const problem = checkFile(file);
      if (problem) { toast(`${file.name}: ${problem}`, "error"); return; }
      const ok = await confirmDialog({ title: `Zastąpić ${f.name}?`, confirmLabel: "Zastąp",
        text: `Nowa wersja: ${file.name} (${fmtSize(file.size)}). Nazwa w bazie wiedzy zostaje ta sama; obecna wersja trafi do kosza na ${state.info.limits.trash_days} dni.` });
      if (!ok) return;
      const item = { id: state.nextId++, name: `${f.name} (nowa wersja)`, size: file.size, progress: 0, phase: "send", source: f.source };
      state.uploads.push(item);
      renderUploads();
      try {
        await send("PUT", `/kb/file?source=${enc(f.source)}`, file, (p) => { item.progress = p; renderUploads(); });
        item.phase = "queued";
        toast(`Zastąpiono ${f.name}. Indeksuję nową wersję.`, "success");
      } catch (err) { item.phase = "error"; item.message = err.message; toast(err.message, "error"); }
      renderUploads();
      refresh();
    } });
    document.body.append(input);
    input.click();
  }

  async function remove(f) {
    const ok = await confirmDialog({ title: `Usunąć ${f.name} z bazy wiedzy?`, confirmLabel: "Usuń", danger: true,
      text: `Asystent przestanie korzystać z tego dokumentu. Plik trafi do kosza na ${state.info.limits.trash_days} dni — do tego czasu można go przywrócić.` });
    if (!ok) return;
    try { await api(`/kb/file?source=${enc(f.source)}`, { method: "DELETE" }); } catch (err) { toast(err.message, "error"); return; }
    toast(`Usunięto ${f.name} (w koszu).`, "success");
    refresh();
  }

  async function restore(t) {
    const current = state.info.files.find((f) => f.source === t.source);
    if (current) {
      const ok = await confirmDialog({ title: `Przywrócić tę wersję ${t.name}?`, confirmLabel: "Przywróć",
        text: "W bazie wiedzy jest teraz inna wersja tego dokumentu — trafi do kosza w jej miejsce." });
      if (!ok) return;
    }
    try { await api(`/kb/trash/restore?id=${enc(t.id)}`, { method: "POST" }); } catch (err) { toast(err.message, "error"); return; }
    toast(`Przywrócono ${t.name}. Indeksuję go ponownie.`, "success");
    refresh();
  }

  async function preview(f) {
    const body = h("div", { class: "kb-text" }, h("p", { class: "muted" }, "Wczytuję tekst dokumentu…"));
    const link = (inline) => `${API}/kb/file?source=${enc(f.source)}${inline ? "&inline=1" : ""}`;
    openModal(h("div", {},
      h("div", { class: "modal-head" }, h("div", { class: "kb-modal-icon" }, ICONS[f.ext] || "📄"),
        h("div", { class: "kb-modal-title" }, h("h3", {}, f.name),
          h("p", {}, [f.type, fmtSize(f.size), pagesText(f), f.chunks != null ? plural(f.chunks, "fragment", "fragmenty", "fragmentów") : null]
            .filter(Boolean).join(" · ")))),
      h("div", { class: "modal-body" },
        h("p", { class: "muted kb-text-note" }, "Tak asystent widzi ten dokument: tekst wyciągnięty z pliku, a ze stron zeskanowanych — odczytany przez OCR."),
        body),
      h("div", { class: "modal-foot" },
        INLINE.includes(f.ext) ? h("a", { class: "btn", href: link(true), target: "_blank", rel: "noopener" }, "↗ Otwórz oryginał") : null,
        h("a", { class: "btn", href: link(false) }, "⬇ Pobierz"),
        h("button", { class: "btn primary", onclick: closeModal }, "Zamknij"))));
    try {
      const data = await api(`/kb/text?source=${enc(f.source)}`);
      if (data.error) { body.replaceChildren(h("p", { class: "kb-error-box" }, "⚠️ Nie udało się odczytać tekstu: ", data.error)); return; }
      body.replaceChildren(...data.segments.map((s) => h("div", { class: "kb-seg" },
        s.label || s.ocr ? h("div", { class: "kb-seg-head" }, s.label ? h("b", {}, s.label) : null,
          s.ocr ? h("span", { class: "kb-badge ocr" }, `📷 skan — OCR${s.confidence != null ? `, pewność ${Math.round(s.confidence)}%` : ""}`) : null) : null,
        h("pre", {}, s.text))),
        ...(data.truncated ? [h("p", { class: "muted" }, "… (podgląd skrócony — pełny tekst jest w indeksie)")] : []));
    } catch (err) { body.replaceChildren(h("p", { class: "kb-error-box" }, err.message)); }
  }

  // ------------------------------------------------------------------ rendering
  function pagesText(f) {
    if (f.pages) return plural(f.pages, "strona", "strony", "stron");
    if (f.sheets) return plural(f.sheets, "arkusz", "arkusze", "arkuszy");
    return null;
  }

  const STATUS = {
    indexed: ["ok", "✓ zaindeksowany"], queued: ["wait", "⏳ w kolejce"], indexing: ["run", "⚙️ indeksuję…"],
    pending: ["wait", "🕓 czeka na sprawdzenie folderu"], error: ["bad", "⚠️ błąd"],
    removing: ["wait", "🗑 usuwam z indeksu"],
  };

  function statusPill(f) {
    const [cls, text] = STATUS[f.status] || ["wait", f.status];
    const pill = h("span", { class: `kb-status ${cls}`, title: f.status === "removing"
      ? "Pliku nie ma już w folderze; asystent przestanie z niego korzystać po najbliższym sprawdzeniu folderu." : f.error || "" }, text);
    if (f.status === "error") {
      pill.classList.add("clickable");
      pill.onclick = () => openModal(h("div", {},
        h("div", { class: "modal-head" }, h("div", {}, h("h3", {}, `Nie udało się zaindeksować: ${f.name}`))),
        h("div", { class: "modal-body" }, h("p", { class: "kb-error-box" }, f.error || "Nieznany błąd."),
          h("p", { class: "muted" }, "Najczęstsze przyczyny: plik bez tekstu (skan, gdy OCR jest niedostępny), plik uszkodzony albo zabezpieczony hasłem. Popraw plik i zastąp nim obecny.")),
        h("div", { class: "modal-foot" }, h("button", { class: "btn primary", onclick: closeModal }, "Zamknij"))), true);
    }
    return pill;
  }

  function whoCell(f) {
    let first;
    if (f.via === "panel") first = h("span", {}, "👤 ", h("b", {}, f.added_by || "?"));
    else if (f.via === "czat") first = h("span", {}, "💬 ", h("b", {}, f.added_by || "użytkownik"), " (z czatu)");
    else first = h("span", { class: "muted" }, "📁 folder na serwerze");
    const when = f.added_at || f.modified;
    return h("td", { class: "kb-who" }, first, when ? h("div", { class: "muted small" }, fmtDate(when)) : null,
      f.replaced_by ? h("div", { class: "muted small" }, `🔄 nowa wersja: ${f.replaced_by}, ${fmtDate(f.replaced_at)}`) : null);
  }

  function visibleFiles() {
    const q = state.query.trim().toLowerCase();
    const filters = {
      all: () => true, firma: (f) => f.origin === "firma", dodane: (f) => f.origin === "dodane",
      problem: (f) => ["error", "pending", "removing"].includes(f.status), ocr: (f) => (f.ocr_pages || 0) > 0,
    };
    const { key, dir } = state.sort;
    const value = { name: (f) => f.source.toLowerCase(), size: (f) => f.size, chunks: (f) => f.chunks || 0,
                    added: (f) => f.added_at || f.modified || "" }[key];
    return state.info.files.filter(filters[state.filter] || filters.all)
      .filter((f) => !q || f.source.toLowerCase().includes(q))
      .sort((a, b) => (value(a) < value(b) ? -1 : value(a) > value(b) ? 1 : 0) * dir);
  }

  function sortHead(label, key, cls) {
    const active = state.sort.key === key;
    return h("th", { class: `sortable${cls ? " " + cls : ""}${active ? " active" : ""}`, onclick: () => {
      state.sort = { key, dir: active ? -state.sort.dir : 1 };
      renderTable();
    } }, label, active ? (state.sort.dir > 0 ? " ▲" : " ▼") : "");
  }

  function renderTable() {
    const box = $("#kb-table");
    const files = visibleFiles();
    if (!state.info.files.length) {
      box.replaceChildren(h("div", { class: "empty-state kb-empty" }, h("div", { class: "empty-icon" }, "📚"),
        h("p", {}, "Baza wiedzy jest pusta."),
        h("p", { class: "muted" }, "Dodaj dokumenty przyciskiem powyżej albo przeciągnij je tutaj.")));
      return;
    }
    if (!files.length) { box.replaceChildren(h("p", { class: "muted kb-none" }, "Żaden dokument nie pasuje do wyszukiwania.")); return; }
    box.replaceChildren(h("table", { class: "kb-table" },
      h("thead", {}, h("tr", {}, sortHead("Dokument", "name"), h("th", {}, "Typ"), sortHead("Rozmiar", "size", "num"),
        h("th", { class: "num" }, "Strony"), sortHead("Fragmenty", "chunks", "num"), h("th", {}, "OCR"),
        sortHead("Dodano", "added"), h("th", {}, "Stan"), h("th", { class: "kb-actions-head" }, ""))),
      h("tbody", {}, files.map((f) => h("tr", { class: f.status === "error" ? "has-error" : "" },
        h("td", { class: "kb-name", "data-label": "Dokument" },
          h("span", { class: "kb-icon" }, ICONS[f.ext] || "📄"),
          h("div", { class: "kb-name-text" },
            f.status === "removing" ? h("span", { class: "kb-link gone" }, f.name)
              : h("button", { class: "kb-link", title: `Podgląd tekstu: ${f.source}`, onclick: () => preview(f) }, f.name),
            f.folder || f.origin === "dodane" ? h("div", { class: "muted small" },
              f.origin === "dodane" ? h("span", { class: "kb-badge chat" }, "dodany z czatu") : null,
              f.folder && f.origin !== "dodane" ? `📁 ${f.folder}` : null) : null)),
        h("td", { "data-label": "Typ" }, f.type),
        h("td", { class: "num", "data-label": "Rozmiar" }, f.status === "removing" ? "—" : fmtSize(f.size)),
        h("td", { class: "num", "data-label": "Strony" }, f.pages || (f.sheets ? `${f.sheets} ark.` : "—")),
        h("td", { class: "num", "data-label": "Fragmenty" }, f.chunks != null ? String(f.chunks) : "—"),
        h("td", { "data-label": "OCR" }, f.ocr_pages ? h("span", { class: "kb-badge ocr",
          title: "Strony zeskanowane, odczytane przez OCR" + (f.ocr_confidence != null ? ` (średnia pewność ${Math.round(f.ocr_confidence)}%)` : "") },
          `📷 ${f.ocr_pages} str.` + (f.ocr_confidence != null ? ` · ${Math.round(f.ocr_confidence)}%` : "")) : h("span", { class: "muted" }, "—")),
        whoCell(f),
        h("td", { "data-label": "Stan" }, statusPill(f)),
        h("td", { class: "kb-row-actions" }, f.status === "removing" ? null : [
          h("button", { class: "icon-btn", title: "Podgląd tekstu", onclick: () => preview(f) }, "👁"),
          h("a", { class: "icon-btn", title: "Pobierz oryginał", href: `${API}/kb/file?source=${enc(f.source)}` }, "⬇")],
          f.writable ? h("button", { class: "icon-btn", title: "Zastąp nową wersją", onclick: () => replace(f) }, "🔄") : null,
          f.writable ? h("button", { class: "icon-btn danger", title: "Usuń (do kosza)", onclick: () => remove(f) }, "🗑") : null))))));
  }

  function renderStats() {
    const i = state.info;
    const q = i.queue;
    const scan = i.last_scan;
    const work = q.running ? `⚙️ indeksuję: ${q.running === "*" ? "sprawdzam folder" : q.running}` + (q.waiting ? ` (+${q.waiting} w kolejce)` : "")
      : q.waiting ? `⏳ ${plural(q.waiting, "dokument czeka", "dokumenty czekają", "dokumentów czeka")} na indeksowanie` : null;
    $("#kb-stats").replaceChildren(
      h("div", { class: "stat" }, h("b", {}, String(i.totals.files)), " " + plural(i.totals.files, "dokument", "dokumenty", "dokumentów").replace(/^\d+ /, "")),
      h("div", { class: "stat" }, h("b", {}, String(i.totals.chunks)), " fragm. w indeksie"),
      h("div", { class: "stat" }, h("b", {}, String(i.totals.ocr_pages)), " str. ze skanów (OCR)"),
      h("div", { class: "stat" }, h("b", {}, fmtSize(i.totals.size)), " na dysku"),
      h("div", { class: "stat wide" + (work ? " busy" : "") }, work ? h("b", {}, work)
        : h("span", {}, "📁 Folder sprawdzony ", h("b", {}, scan ? ago(scan.at) : "—"),
            i.limits.rescan_minutes ? ` · automatycznie co ${i.limits.rescan_minutes} min` : "")));
  }

  function renderUploads() {
    const box = $("#kb-uploads");
    if (!box) return;
    const items = state.uploads;
    if (!items.length) { box.hidden = true; box.replaceChildren(); return; }
    box.hidden = false;
    const finished = items.every((u) => ["done", "error", "skipped"].includes(u.phase));
    const label = { wait: "czeka na wysłanie", send: null, queued: "wysłany — w kolejce do indeksowania",
                    done: null, error: null, skipped: null };
    box.replaceChildren(
      h("div", { class: "kb-uploads-head" }, h("b", {}, "Wysyłanie dokumentów"),
        finished ? h("button", { class: "btn small ghost", onclick: () => { state.uploads = []; renderUploads(); } }, "Wyczyść listę") : null),
      ...items.map((u) => h("div", { class: `kb-upload ${u.phase}` },
        h("span", { class: "kb-icon" }, ICONS[ext(u.name)] || "📄"),
        h("div", { class: "kb-upload-main" },
          h("div", { class: "kb-upload-name" }, h("span", {}, u.name), h("span", { class: "muted small" }, fmtSize(u.size))),
          u.phase === "send" ? h("div", { class: "kb-progress" }, h("div", { style: { width: `${Math.round(u.progress * 100)}%` } })) : null,
          h("div", { class: "small kb-upload-msg" },
            u.phase === "send" ? `wysyłanie ${Math.round(u.progress * 100)}%` : u.phase === "done" ? `✓ ${u.message}`
              : u.phase === "error" ? `⚠️ ${u.message}` : u.phase === "skipped" ? u.message : label[u.phase])))));
  }

  function renderTrash() {
    const items = state.info.trash;
    const box = $("#kb-trash");
    const days = state.info.limits.trash_days;
    box.replaceChildren(h("details", { class: "kb-trash", open: box.dataset.open === "1" ? true : null,
      ontoggle: (ev) => { box.dataset.open = ev.target.open ? "1" : ""; } },
      h("summary", {}, `🗑️ Kosz (${items.length})`, h("span", { class: "muted small" }, ` — usunięte i zastąpione wersje są przechowywane ${days} dni`)),
      items.length ? h("table", { class: "kb-table kb-trash-table" },
        h("tbody", {}, items.map((t) => h("tr", {},
          h("td", { class: "kb-name" }, h("span", { class: "kb-icon" }, ICONS[ext(t.name)] || "📄"),
            h("div", { class: "kb-name-text" }, h("span", {}, t.source), h("div", { class: "muted small" },
              t.reason === "replaced" ? "zastąpiony nowszą wersją" : "usunięty"))),
          h("td", { class: "num" }, fmtSize(t.size)),
          h("td", {}, `👤 ${t.by}`, h("div", { class: "muted small" }, fmtDate(t.at))),
          h("td", { class: "muted" }, t.days_left > 0 ? `jeszcze ${plural(t.days_left, "dzień", "dni", "dni")}` : "do usunięcia przy najbliższym sprawdzeniu"),
          h("td", { class: "kb-row-actions" }, state.info.writable ? h("button", { class: "btn small", onclick: () => restore(t) }, "↩️ Przywróć") : null)))))
        : h("p", { class: "muted" }, "Kosz jest pusty.")));
  }

  function render() {
    if (!state.built) build();
    renderStats();
    // the list is polled every 2 s while documents are indexed: redraw it only when it changed, so a click never
    // lands on a row that was just replaced
    const files = JSON.stringify([state.info.files, state.info.writable]);
    if (files !== state.drawnFiles) { state.drawnFiles = files; renderTable(); }
    const trash = JSON.stringify(state.info.trash);
    if (trash !== state.drawnTrash) { state.drawnTrash = trash; renderTrash(); }
    renderUploads();
    $("#kb-readonly").hidden = state.info.writable;
    $("#kb-add").disabled = !state.info.writable;
  }

  function build() {
    const root = $("#kb-root");
    const input = h("input", { type: "file", multiple: true, hidden: true, accept: state.info.limits.types.join(","),
                               onchange: (ev) => { addFiles([...ev.target.files]); ev.target.value = ""; } });
    const drop = h("div", { class: "kb-drop-overlay" }, h("div", {}, h("div", { class: "empty-icon" }, "📥"), "Upuść pliki, aby dodać je do bazy wiedzy"));
    root.replaceChildren(
      h("div", { class: "kb-head" },
        h("div", {}, h("h2", {}, "📚 Baza wiedzy"),
          h("p", { class: "muted" }, "Dokumenty, na podstawie których asystent odpowiada wszystkim użytkownikom. Nowe pliki trafiają do folderu ",
            "dokumentów firmy na serwerze i są indeksowane w tle — zeskanowane strony odczytuje OCR. Pliki skopiowane do folderu ",
            "bezpośrednio na serwerze są wykrywane automatycznie.")),
        h("div", { class: "kb-head-actions" }, input,
          h("button", { class: "btn primary", id: "kb-add", onclick: () => input.click() }, "⬆️ Dodaj dokumenty"),
          h("button", { class: "btn", title: "Wykryj pliki dodane, zmienione lub usunięte w folderze na serwerze", onclick: rescanNow }, "🔄 Sprawdź folder teraz"))),
      h("div", { class: "stats kb-stats", id: "kb-stats" }),
      h("div", { class: "tpl-banner kb-readonly", id: "kb-readonly", hidden: true },
        "Folder dokumentów to przykładowe dokumenty z repozytorium Hapilota — można je przeglądać, ale nie zmieniać. ",
        "Ustaw własny folder (HAPILOT_DOCS_DIR w .env), aby dodawać, zastępować i usuwać dokumenty w panelu."),
      h("div", { class: "kb-uploads", id: "kb-uploads", hidden: true }),
      h("div", { class: "kb-toolbar" },
        h("input", { class: "search", type: "search", placeholder: "Szukaj dokumentu…", autocomplete: "off",
                     oninput: (ev) => { state.query = ev.target.value; renderTable(); } }),
        h("select", { class: "kb-filter", onchange: (ev) => { state.filter = ev.target.value; renderTable(); } },
          [["all", "Wszystkie dokumenty"], ["firma", "Dokumenty firmy (folder)"], ["dodane", "Dodane z czatu"],
           ["ocr", "Ze skanami (OCR)"], ["problem", "Wymagające uwagi"]].map(([v, t]) => h("option", { value: v }, t))),
        h("span", { class: "muted small kb-limits" }, `Typy: ${state.info.limits.types.join(", ")} · do ${state.info.limits.max_mb} MB na plik`)),
      h("div", { class: "kb-table-wrap", id: "kb-table" }),
      h("div", { id: "kb-trash" }),
      h("p", { class: "muted small kb-folder" }, `Folder dokumentów na serwerze: ${state.info.folders.docs} · dodane z czatu: ${state.info.folders.added}`),
      drop);
    let depth = 0;
    root.addEventListener("dragenter", (ev) => { if (!ev.dataTransfer || ![...ev.dataTransfer.types].includes("Files")) return; ev.preventDefault(); depth++; root.classList.add("dragging"); });
    root.addEventListener("dragover", (ev) => { if (root.classList.contains("dragging")) ev.preventDefault(); });
    root.addEventListener("dragleave", () => { depth = Math.max(0, depth - 1); if (!depth) root.classList.remove("dragging"); });
    root.addEventListener("drop", (ev) => {
      if (!root.classList.contains("dragging")) return;
      ev.preventDefault();
      depth = 0;
      root.classList.remove("dragging");
      addFiles([...ev.dataTransfer.files]);
    });
    state.built = true;
  }

  async function open() {
    if (!A.isAdmin()) {
      $("#kb-root").replaceChildren(h("div", { class: "empty-state" }, h("p", {}, "Bazą wiedzy zarządza administrator.")));
      return;
    }
    await refresh();
  }

  async function init() {
    try { if ((await api("/me")).admin) $("#knowledge-tab").hidden = false; } catch (e) { /* shown elsewhere */ }
  }

  window.HapilotKnowledge = { open, dirty: () => state.uploads.some((u) => ["wait", "send"].includes(u.phase)) };
  init();
})();
