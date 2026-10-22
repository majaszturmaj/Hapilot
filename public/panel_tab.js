// A permanent tab for the right-hand panel "📎 Źródła i pliki" (src/ui_panel.py).
// Chainlit's sidebar has no "reopen" control: its ← button drops the panel. This tab stays on the
// screen once an answer has sources or files. Closed panel: the tab sits on the right edge and
// reopens the panel on the answer viewed last. Open panel: the tab sits on the panel's left edge
// and minimises it. Loaded through custom_js in .chainlit/config.toml.
(function () {
  const LABEL = "📎 Źródła i pliki";
  const TITLE_ID = "side-view-title";

  // one "📎 Źródła i pliki" button under every answer with a panel turn, in order: button i = turn id i + 1
  const turnButtons = () =>
    [...document.querySelectorAll("button")].filter((b) => b.textContent.trim() === LABEL && !b.closest("#hapilot-panel-tab"));
  const panelTitle = () => {
    const el = document.getElementById(TITLE_ID);
    return el && el.textContent.includes(LABEL) && el.offsetParent !== null ? el : null;
  };

  const tab = document.createElement("button");
  tab.id = "hapilot-panel-tab";
  tab.type = "button";
  Object.assign(tab.style, {
    position: "fixed", top: "50%", right: "0", transform: "translateY(-50%)", zIndex: "40",
    writingMode: "vertical-rl", padding: "14px 7px", fontSize: "13px", fontWeight: "600", cursor: "pointer",
    border: "1px solid hsl(var(--border))", borderRight: "none", borderRadius: "8px 0 0 8px",
    background: "hsl(var(--primary))", color: "hsl(var(--primary-foreground))",
    boxShadow: "0 2px 8px rgba(0,0,0,.25)", display: "none",
  });

  function update() {
    const buttons = turnButtons();
    const title = panelTitle();
    tab.style.display = buttons.length || title ? "block" : "none";
    if (title) {
      const panel = title.closest("aside") || title.parentElement;
      tab.style.right = `${Math.max(0, window.innerWidth - panel.getBoundingClientRect().left)}px`;
      tab.textContent = "Zwiń ▸";
      tab.title = "Zwiń panel źródeł i plików";
    } else {
      tab.style.right = "0";
      tab.textContent = "◂ " + LABEL;
      tab.title = "Pokaż źródła i pliki";
    }
  }

  tab.addEventListener("click", () => {
    const title = panelTitle();
    if (title) {
      const close = title.querySelector("button");
      if (close) close.click();
    } else {
      const buttons = turnButtons();
      const wanted = Number(window.hapilotPanelTurn || 0);
      const button = (wanted && buttons[wanted - 1]) || buttons[buttons.length - 1];
      if (button) button.click();
    }
    setTimeout(update, 50);
  });

  function start() {
    document.body.appendChild(tab);
    let pending = false;
    new MutationObserver(() => {
      if (!pending) {
        pending = true;
        requestAnimationFrame(() => { pending = false; update(); });
      }
    }).observe(document.body, { childList: true, subtree: true });
    window.addEventListener("resize", update);
    update();
  }

  if (document.body) start();
  else document.addEventListener("DOMContentLoaded", start);
})();

// Link to the agent panel (/hapilot/admin, src/ui_admin.py) for logged-in users: placed in Chainlit's
// #header, before the "O Hapilocie" button (#readme-button); administrators see it labelled as such.
(function () {
  let canOpen = null;
  const link = document.createElement("a");
  link.id = "hapilot-admin-link";
  link.href = "/hapilot/admin";
  link.textContent = "🕸️ Panel agenta";
  Object.assign(link.style, {
    fontSize: "13px", fontWeight: "600", padding: "6px 12px", borderRadius: "9px", textDecoration: "none",
    border: "1px solid hsl(var(--border))", color: "hsl(var(--foreground))", marginRight: "6px", whiteSpace: "nowrap",
  });

  function place() {
    if (!canOpen || document.getElementById("hapilot-admin-link")) return;
    const about = document.getElementById("readme-button");  // Chainlit's "O Hapilocie" button in #header
    if (about && about.parentElement) about.parentElement.insertBefore(link, about);
  }

  async function check() {
    try {
      const res = await fetch("/hapilot/admin/api/me", { credentials: "same-origin" });
      const me = res.ok ? await res.json() : null;
      canOpen = Boolean(me);  // any logged-in user may open the panel
      if (me) link.textContent = me.admin ? "⚙️ Panel agenta (administrator)" : "🕸️ Panel agenta";
    } catch (e) { canOpen = false; }
    if (!canOpen) { const old = document.getElementById("hapilot-admin-link"); if (old) old.remove(); }
    place();
  }

  function start() {
    check();
    new MutationObserver(() => { if (canOpen) place(); }).observe(document.body, { childList: true, subtree: true });
    window.addEventListener("focus", check);
  }

  if (document.body) start();
  else document.addEventListener("DOMContentLoaded", start);
})();
