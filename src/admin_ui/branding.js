/* Hapilot agent panel — the "🎨 Wygląd" tab (administrators): the company's name, description, logo and main colour,
   the same for every user. API: /hapilot/admin/api/branding (src/ui_admin.py → src/branding.py). Helpers come from
   admin.js. Every logged-in user gets the current name and logo in the panel's top bar. */
(() => {
  "use strict";

  const A = window.HapilotAdmin;
  const { h, api, toast, confirmDialog, fmtDate } = A;
  const $ = (sel, root = document) => root.querySelector(sel);
  const state = { info: null, draft: null, logoData: null, dirty: false };

  // ------------------------------------------------------------------ top bar (everyone)
  function applyTopbar(info) {
    const mark = $("#brand-mark");
    if (info.logo_url) {
      mark.replaceChildren(h("img", { src: info.logo_url, alt: "" }));
      mark.classList.add("logo");
    } else {
      mark.replaceChildren((info.name || "H")[0].toUpperCase());
      mark.classList.remove("logo");
    }
    $("#brand-name").textContent = info.name;
    document.title = `${info.name} · Panel agenta`;
    const link = document.querySelector('link[data-branding]');
    if (link) link.href = "/hapilot/branding.css?t=" + Date.now();  // the colour, without a stale copy
  }

  async function init() {
    try { state.info = await api("/branding"); } catch (e) { return; }
    applyTopbar(state.info);
    if (state.info.admin) $("#branding-tab").hidden = false;
  }

  // ------------------------------------------------------------------ form (administrators)
  function resetDraft() {
    const i = state.info;
    state.draft = { name: i.name, description: i.description || "", color: i.color, logo: i.logo_url, removeLogo: false };
    state.logoData = null;
    state.dirty = false;
  }

  function mark(dirty = true) { state.dirty = dirty; renderPreview(); $("#brand-save").disabled = !dirty; }

  function readLogo(file) {
    if (!file) return;
    if (!["image/png", "image/jpeg"].includes(file.type)) { toast("Logo musi być plikiem PNG albo JPEG.", "error"); return; }
    if (file.size > state.info.limits.logo_bytes) { toast("Logo jest za duże (najwyżej 1 MB).", "error"); return; }
    const reader = new FileReader();
    reader.onload = () => {
      state.logoData = reader.result;
      state.draft.logo = reader.result;
      state.draft.removeLogo = false;
      renderLogoField();
      mark();
    };
    reader.readAsDataURL(file);
  }

  function renderLogoField() {
    const d = state.draft;
    const box = $("#brand-logo-box");
    const input = h("input", { type: "file", accept: "image/png,image/jpeg", hidden: true,
                               onchange: (ev) => readLogo(ev.target.files[0]) });
    box.replaceChildren(
      d.logo ? h("div", { class: "brand-logo-preview" }, h("img", { src: d.logo, alt: "Logo" }))
             : h("div", { class: "brand-logo-preview empty" }, "Brak własnego logo — używane jest logo Hapilota"),
      h("div", { class: "brand-logo-buttons" },
        input,
        h("button", { type: "button", class: "btn small", onclick: () => input.click() }, d.logo ? "🔄 Zmień logo" : "⬆️ Wgraj logo"),
        d.logo ? h("button", { type: "button", class: "btn small danger-ghost", onclick: () => {
          d.logo = null; d.removeLogo = true; state.logoData = null; renderLogoField(); mark();
        } }, "Usuń logo") : null));
  }

  // the favicon and the avatar show the logo only when it is roughly square (src/branding.py, icon())
  function avatarPreview(d) {
    const el = h("div", { class: "pv-avatar" }, (d.name || "H")[0].toUpperCase());
    if (d.logo) {
      const img = new Image();
      img.onload = () => { const r = img.naturalWidth / img.naturalHeight; if (r >= 0.67 && r <= 1.5) el.replaceChildren(h("img", { src: d.logo, alt: "" })); };
      img.src = d.logo;
    }
    return el;
  }

  function renderPreview() {
    const d = state.draft;
    const pane = $("#brand-preview");
    if (!pane) return;
    const logo = d.logo ? h("img", { src: d.logo, alt: "" }) : h("span", { class: "pv-mark" }, (d.name || "H")[0].toUpperCase());
    pane.style.setProperty("--pv", d.color);
    pane.replaceChildren(...[  // replaceChildren would print a null as the text "null"
      h("div", { class: "pv-title" }, "Podgląd"),
      h("div", { class: "pv-card pv-login" },
        h("div", { class: "pv-logo" }, logo),
        h("div", { class: "pv-heading" }, `Zaloguj się — ${d.name || "…"}`),
        h("div", { class: "pv-input" }, "nazwa użytkownika"),
        h("div", { class: "pv-button" }, "Zaloguj się")),
      h("div", { class: "pv-card pv-chat" },
        h("div", { class: "pv-msg" },
          avatarPreview(d),
          h("div", {}, h("div", { class: "pv-author" }, d.name || "…"),
            h("div", { class: "pv-text" }, `👋 Cześć! Jestem ${d.name || "…"} — Twój lokalny asystent AI.`))),
        h("div", { class: "pv-send" }, h("span", {}, "Napisz wiadomość…"), h("span", { class: "pv-dot" }, "↑"))),
      d.description ? h("div", { class: "pv-card pv-prompt" },
        h("div", { class: "pv-label" }, "Fragment promptu asystenta (niewidoczny dla użytkowników)"),
        h("pre", {}, `[FIRMA]\n${d.description}\nPrzedstawiając się, używaj nazwy: ${d.name}.`)) : null,
      h("p", { class: "muted pv-note" }, "Po zapisaniu nowy wygląd obowiązuje u wszystkich użytkowników od następnego odświeżenia strony.")]
      .filter(Boolean));
  }

  function field(label, hint, control, extra) {
    return h("div", { class: "field" }, h("label", {}, label), control, extra || null, hint ? h("div", { class: "hint" }, hint) : null);
  }

  function renderForm() {
    const d = state.draft;
    const limits = state.info.limits;
    const counter = h("span", { class: "brand-counter" }, `${d.description.length}/${limits.description}`);
    const colorText = h("input", { type: "text", class: "mono brand-hex", value: d.color, maxlength: 7, spellcheck: "false",
      oninput: (ev) => { if (/^#[0-9a-fA-F]{6}$/.test(ev.target.value)) { d.color = ev.target.value.toLowerCase(); colorPick.value = d.color; mark(); } } });
    const colorPick = h("input", { type: "color", value: d.color,
      oninput: (ev) => { d.color = ev.target.value; colorText.value = d.color; mark(); } });
    return h("form", { class: "brand-form", onsubmit: (ev) => { ev.preventDefault(); save(); } },
      h("h2", {}, "🎨 Wygląd aplikacji"),
      h("p", { class: "muted" }, "Dopasuj aplikację do firmy, w której działa. Zmiany obowiązują u wszystkich użytkowników; ",
        "dotyczą tylko czatu — przebiegi ewaluacyjne zawsze działają jako Hapilot."),
      field("Nazwa aplikacji",
        "Tytuł strony, ekran logowania, nazwa asystenta przy wiadomościach i w promptach (zamiast „Hapilot”). Litery, cyfry, spacje i . , ( ) + / ! ? : -",
        h("input", { type: "text", value: d.name, maxlength: limits.name, required: true,
                     oninput: (ev) => { d.name = ev.target.value; mark(); } })),
      field("Opis firmy dla asystenta",
        "1–2 zdania o firmie. Trafia do promptów jako sekcja [FIRMA], żeby asystent wiedział, dla kogo pracuje. Użytkownicy go nie widzą.",
        h("textarea", { rows: 3, maxlength: limits.description,
                        placeholder: "Np. Przykładowa Sp. z o.o. projektuje i serwisuje maszyny dla przemysłu wydobywczego.",
                        oninput: (ev) => { d.description = ev.target.value; counter.textContent = `${d.description.length}/${limits.description}`; mark(); } },
          d.description),
        counter),
      field("Logo",
        "PNG albo JPEG do 1 MB (SVG nie jest przyjmowany ze względów bezpieczeństwa). Najlepiej na przezroczystym tle i czytelne zarówno na jasnym, jak i ciemnym motywie. Pokazuje się w nagłówku i na ekranie logowania; jako ikona karty i awatar asystenta — gdy jest mniej więcej kwadratowe (inaczej pierwsza litera nazwy na kolorze głównym).",
        h("div", { id: "brand-logo-box", class: "brand-logo-box" })),
      field("Kolor główny", "Przyciski i akcenty czatu oraz panelu.",
        h("div", { class: "brand-color-row" }, colorPick, colorText,
          h("button", { type: "button", class: "btn small ghost", onclick: () => {
            d.color = state.info.default.color; colorPick.value = d.color; colorText.value = d.color; mark();
          } }, "Domyślny (zieleń Hapilota)"))),
      h("div", { class: "brand-actions" },
        h("button", { type: "submit", class: "btn primary", id: "brand-save", disabled: true }, "💾 Zapisz dla wszystkich"),
        h("button", { type: "button", class: "btn", onclick: () => { resetDraft(); render(); } }, "Odrzuć zmiany"),
        state.info.customised ? h("button", { type: "button", class: "btn danger-ghost", onclick: resetAll }, "↩️ Przywróć wygląd Hapilota") : null));
  }

  function renderHistory() {
    const rows = state.info.history || [];
    const box = h("div", { class: "brand-history" }, h("h3", {}, "Historia zmian"));
    if (!rows.length) { box.append(h("p", { class: "muted" }, "Wygląd nie był jeszcze zmieniany.")); return box; }
    rows.forEach((v, i) => {
      box.append(h("div", { class: "brand-version" + (i === 0 ? " current" : "") },
        v.reset ? h("span", { class: "brand-swatch", style: { background: state.info.default.color } })
                : h("span", { class: "brand-swatch", style: { background: v.color } }),
        h("div", { class: "brand-version-text" },
          h("b", {}, v.reset ? "Wygląd Hapilota (przywrócony)" : v.name),
          h("span", { class: "muted" }, `${fmtDate(v.ts)} · ${v.user}${v.logo ? " · z logo" : ""}${i === 0 ? " · obecny" : ""}`)),
        i > 0 && !v.reset ? h("button", { class: "btn small", onclick: () => restore(v.index) }, "Przywróć") : null));
    });
    return box;
  }

  function render() {
    const root = $("#brand-root");
    if (!state.info.admin) {
      root.replaceChildren(h("div", { class: "empty-state" }, h("p", {}, "Wygląd aplikacji zmienia administrator.")));
      return;
    }
    root.replaceChildren(
      h("div", { class: "brand-main" }, renderForm(), renderHistory()),
      h("aside", { class: "brand-preview", id: "brand-preview" }));
    renderLogoField();
    renderPreview();
  }

  // ------------------------------------------------------------------ actions
  async function save() {
    const d = state.draft;
    const body = { name: d.name, description: d.description, color: d.color };
    if (state.logoData) body.logo = state.logoData;
    else if (d.removeLogo) body.logo = "remove";
    try {
      state.info = await api("/branding", { method: "PUT", body });
    } catch (err) { toast(err.message, "error"); return; }
    resetDraft();
    applyTopbar(state.info);
    render();
    toast("Zapisano. Nowy wygląd obowiązuje u wszystkich od odświeżenia strony czatu.", "success");
  }

  async function resetAll() {
    const ok = await confirmDialog({ title: "Przywrócić wygląd Hapilota?",
      text: "Nazwa, opis firmy, logo i kolor wrócą do ustawień Hapilota u wszystkich użytkowników. Obecna wersja zostaje w historii.",
      confirmLabel: "Przywróć", danger: true });
    if (!ok) return;
    try { state.info = await api("/branding", { method: "DELETE" }); } catch (err) { toast(err.message, "error"); return; }
    resetDraft();
    applyTopbar(state.info);
    render();
    toast("Przywrócono wygląd Hapilota.", "success");
  }

  async function restore(index) {
    try { state.info = await api(`/branding/restore/${index}`, { method: "POST" }); } catch (err) { toast(err.message, "error"); return; }
    resetDraft();
    applyTopbar(state.info);
    render();
    toast("Przywrócono wybraną wersję wyglądu.", "success");
  }

  async function open() {
    try { state.info = await api("/branding"); } catch (err) { toast(err.message, "error"); return; }
    if (!state.dirty) resetDraft();
    render();
  }

  window.HapilotBranding = { open, dirty: () => state.dirty };
  init();
})();
