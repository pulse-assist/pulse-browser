// Browser plugin page: sessions, owner login via screenshot overlay, pending confirms.
const app = document.getElementById("app");
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let data = null;
let error = "";
let busy = "";
let selected = "";
let view = null;
let poll = null;
let showCreate = false;
let confirmDelete = false;
let tabs = [];
let lastFrame = null;   // последний кадр screencast {image_base64, mime, width, height, seq}
let liveGen = 0;        // поколение live-цикла: смена сессии останавливает старый
let inputChain = Promise.resolve();

async function load() {
  try {
    data = await pulse.plugin("/state");
    error = "";
    if (!selected && data.sessions.length) selected = data.sessions[0].name;
    if (selected) await refreshView();
  } catch (e) {
    if (!data) {
      app.innerHTML = `<div class="pl-error">Плагин не отвечает: ${esc(e.message)}</div>`;
      return;
    }
    error = e.message;
  }
  render();
}

async function refreshView() {
  if (!selected) { view = null; return; }
  try {
    view = await pulse.plugin(`/sessions/${encodeURIComponent(selected)}/view`);
  } catch (e) {
    view = null;
    // session may be stopped — not fatal
    if (!String(e.message || "").includes("не найдена")) error = e.message;
  }
}

const q = (name) => encodeURIComponent(name);

// Live view: long-poll кадров screencast (до ~10 к/с) + обновление вкладок раз в 3 с.
function startPoll() {
  const gen = ++liveGen;
  const cur = (data?.sessions || []).find((x) => x.name === selected);
  if (!cur || !cur.running) return;
  const name = selected;
  let seq = 0;
  (async () => {
    while (gen === liveGen) {
      try {
        const f = await pulse.plugin(`/sessions/${q(name)}/frame`, { params: { since: seq, wait: 2 } });
        if (gen !== liveGen) return;
        if (f.changed || !lastFrame) {
          seq = f.seq;
          lastFrame = f;
          paintFrame();
        }
      } catch (e) {
        if (gen !== liveGen) return;
        await new Promise((r) => setTimeout(r, 1500));
      }
    }
  })();
  (async () => {
    while (gen === liveGen) {
      try {
        const t = await pulse.plugin(`/sessions/${q(name)}/tabs`);
        if (gen !== liveGen) return;
        if (JSON.stringify(t.tabs) !== JSON.stringify(tabs)) {
          tabs = t.tabs;
          paintTabs();
        }
      } catch { /* сессия могла закрыться */ }
      await new Promise((r) => setTimeout(r, 3000));
    }
  })();
}

function paintFrame() {
  const img = document.getElementById("frame");
  if (!img || !lastFrame?.image_base64) return;
  img.hidden = false;
  img.src = `data:${lastFrame.mime || "image/jpeg"};base64,${lastFrame.image_base64}`;
  img.dataset.w = lastFrame.width;
  img.dataset.h = lastFrame.height;
  const empty = document.getElementById("view-empty");
  if (empty) empty.remove();
}

function paintTabs() {
  const bar = document.getElementById("tabbar");
  if (!bar) return;
  bar.innerHTML = renderTabs();
  bindTabs();
  const active = tabs.find((t) => t.active);
  const urlEl = document.getElementById("cur-url");
  if (urlEl && active) urlEl.textContent = active.url || "";
}

// Ввод владельца идёт строго по очереди, чтобы клики и клавиши не обгоняли друг друга.
function sendInput(body) {
  inputChain = inputChain
    .then(() => pulse.plugin(`/sessions/${q(selected)}/input`, { method: "POST", body }))
    .catch((e) => { error = e.message || String(e); const el = document.getElementById("live-error"); if (el) { el.textContent = error; el.hidden = false; } });
  return inputChain;
}

async function act(fn, label) {
  busy = label || "Выполняю…";
  render();
  try {
    await fn();
    error = "";
  } catch (e) {
    error = e.message || String(e);
  }
  await load();
  busy = "";
  render();
  startPoll();
}

function render() {
  if (!data) return;
  const sessions = data.sessions || [];
  const pending = data.pending || [];
  const cur = sessions.find((s) => s.name === selected) || null;

  app.innerHTML = `
    ${busy ? `<div class="working" role="status"><span class="spinner"></span>${esc(busy)}</div>` : ""}
    ${error ? `<div class="pl-error">${esc(error)}</div>` : ""}
    <div class="cols">
      <section class="box">
        <div class="box-head">
          <h2>Сессии</h2>
          <span class="hint">профиль = куки; прокси свой</span>
          <span class="grow"></span>
          <button class="btn btn-primary" data-toggle-create>+ Сессия</button>
        </div>
        ${showCreate ? renderCreate() : ""}
        ${sessions.length
          ? sessions.map(renderSession).join("")
          : `<div class="pl-empty">Сессий нет. Создайте профиль (например amazon) и войдите сами — пароль в чат агенту не передавайте.</div>`}
        ${pending.length ? renderPending(pending) : ""}
        <div class="box-foot hint" style="margin-top:0.75rem">Артефакты:
          <button class="btn" data-go="/files?path=@browser">@browser</button>
        </div>
      </section>
      <section class="box">
        <div class="box-head">
          <h2>${cur ? esc(cur.name) : "Просмотр"}</h2>
          ${cur ? `<span class="pl-badge ${cur.mode === "write" ? "pl-important" : ""}">${esc(cur.mode)}</span>
            ${cur.proxy ? `<span class="hint">прокси ${esc(cur.proxy)}</span>` : `<span class="hint">без прокси</span>`}
            ${cur.running ? `<span class="pl-badge pl-ok">running</span>` : `<span class="pl-badge">stopped</span>`}` : ""}
        </div>
        ${cur ? renderViewer(cur) : `<div class="pl-empty">Выберите сессию слева</div>`}
      </section>
    </div>`;
  bind();
}

function renderSession(s) {
  return `<div class="session ${s.name === selected ? "active" : ""}" data-select="${esc(s.name)}">
    <div class="session-name">${esc(s.name)}
      ${s.running ? `<span class="pl-badge pl-ok">on</span>` : ""}
      ${s.locked ? `<span class="pl-badge pl-important">lock</span>` : ""}
    </div>
    <div class="session-meta">${esc(s.url || "—")} · ${esc(s.mode)}${s.proxy ? ` · ${esc(s.proxy)}` : ""}</div>
  </div>`;
}

function renderCreate() {
  return `<form class="form" id="create-form">
    <label>Имя <input name="name" required placeholder="amazon" pattern="[A-Za-z][A-Za-z0-9_-]{0,39}"></label>
    <label>Режим
      <select name="mode">
        <option value="read-only">read-only (агент только смотрит)</option>
        <option value="write">write (агент может кликать)</option>
      </select>
    </label>
    <label>Прокси (необязательно) <input name="proxy" placeholder="http://user:pass@host:port"></label>
    <label>Allowlist доменов <input name="allowlist" placeholder="amazon.com, *.amazon.com"></label>
    <div class="toolbar">
      <button type="submit" class="btn btn-primary">Создать</button>
      <button type="button" class="btn" data-toggle-create>Отмена</button>
    </div>
  </form>`;
}

function renderTabs() {
  return tabs.map((t) => `<span class="tab ${t.active ? "active" : ""}" data-tab="${esc(t.id)}" title="${esc(t.url)}">
      <span class="tab-title">${esc(t.title || t.url || "Новая вкладка")}</span>
      <button class="tab-x" data-tab-close="${esc(t.id)}" aria-label="Закрыть вкладку">×</button>
    </span>`).join("") + `<button class="btn tab-new" data-tab-new title="Новая вкладка">+</button>`;
}

function renderViewer(cur) {
  const src = lastFrame?.image_base64
    ? `data:${lastFrame.mime || "image/jpeg"};base64,${lastFrame.image_base64}`
    : (view?.image_base64 ? `data:image/png;base64,${view.image_base64}` : "");
  return `
    ${cur.running ? `<div class="tabbar" id="tabbar">${renderTabs()}</div>` : ""}
    <div class="toolbar">
      <input type="url" id="nav-url" placeholder="https://…" value="${esc(view?.url || cur.url || "")}">
      <button class="btn btn-primary" data-open>Открыть</button>
      <button class="btn" data-close ${cur.running ? "" : "disabled"}>Закрыть Chrome</button>
    </div>
    <div class="toolbar">
      <label class="hint">Режим агента</label>
      <select id="mode-select">
        <option value="read-only" ${cur.mode === "read-only" ? "selected" : ""}>read-only</option>
        <option value="write" ${cur.mode === "write" ? "selected" : ""}>write</option>
      </select>
      <button class="btn" data-save-mode>Сохранить режим</button>
      ${confirmDelete
        ? `<span class="hint">Удалить профиль?</span>
           <button class="btn btn-primary" data-delete-yes>Да</button>
           <button class="btn" data-delete-no>Нет</button>`
        : `<button class="btn" data-delete>Удалить сессию</button>`}
    </div>
    <div class="hint" id="cur-url">${esc(view?.url || cur.url || "Откройте URL — войдите в аккаунт глазами здесь")}</div>
    <div class="pl-error" id="live-error" hidden></div>
    <div class="view-wrap" id="view-wrap" tabindex="0">
      ${src
        ? `<img id="frame" src="${src}" data-w="${lastFrame?.width || 1280}" data-h="${lastFrame?.height || 800}" alt="live" draggable="false">`
        : `<img id="frame" alt="live" draggable="false" hidden><div class="pl-empty view-empty" id="view-empty">${cur.running ? "Жду кадр…" : "Chrome не запущен. Введите URL и нажмите «Открыть»."}</div>`}
    </div>
    <div class="toolbar" style="margin-top:0.5rem">
      <input type="text" id="type-box" placeholder="Вставить текст в выбранное поле (логин — только здесь)">
      <button class="btn" data-type>Ввести</button>
      <button class="btn" data-enter>Enter</button>
    </div>
    <p class="hint">Живой просмотр: кликайте по кадру, крутите колесо, печатайте — после клика по кадру клавиатура идёт в Chrome. Пароли и 2FA вводите только здесь, не в чат агенту.</p>`;
}

function renderPending(items) {
  return `<div class="pending"><div class="box-head"><h2>Нужен confirm</h2></div>
    ${items.map((p) => `<div class="pending-item">
      <span class="pl-badge pl-urgent">confirm</span>
      <span class="pl-grow">${esc(p.session)} · ${esc(p.action)} · ${esc(p.detail?.url || "")}</span>
      <button class="btn btn-primary" data-confirm="${esc(p.id)}" data-confirm-session="${esc(p.session)}">Разрешить</button>
    </div>`).join("")}
  </div>`;
}

function bind() {
  app.querySelectorAll("[data-select]").forEach((el) => {
    el.addEventListener("click", async () => {
      selected = el.getAttribute("data-select");
      lastFrame = null; tabs = [];
      await refreshView();
      render();
      startPoll();
    });
  });
  app.querySelectorAll("[data-toggle-create]").forEach((el) => {
    el.addEventListener("click", () => { showCreate = !showCreate; render(); });
  });
  app.querySelectorAll("[data-go]").forEach((el) => {
    el.addEventListener("click", () => pulse.navigate(el.getAttribute("data-go")));
  });
  const form = document.getElementById("create-form");
  if (form) {
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      act(async () => {
        const created = await pulse.plugin("/sessions", {
          method: "POST",
          body: {
            name: fd.get("name"),
            mode: fd.get("mode"),
            proxy: fd.get("proxy") || null,
            allowlist: fd.get("allowlist") || "",
          },
        });
        selected = created.name;
        showCreate = false;
      }, "Создаю сессию…");
    });
  }
  const openBtn = app.querySelector("[data-open]");
  if (openBtn) {
    openBtn.addEventListener("click", () => {
      const url = document.getElementById("nav-url").value.trim();
      if (!url) { error = "Укажите URL"; render(); return; }
      act(async () => {
        await pulse.plugin(`/sessions/${encodeURIComponent(selected)}/open`, {
          method: "POST", body: { url },
        });
        await refreshView();
      }, "Открываю…");
    });
  }
  const closeBtn = app.querySelector("[data-close]");
  if (closeBtn) {
    closeBtn.addEventListener("click", () => act(async () => {
      await pulse.plugin(`/sessions/${encodeURIComponent(selected)}/close`, { method: "POST", body: {} });
      view = null;
    }, "Закрываю…"));
  }
  const saveMode = app.querySelector("[data-save-mode]");
  if (saveMode) {
    saveMode.addEventListener("click", () => {
      const mode = document.getElementById("mode-select").value;
      act(async () => {
        await pulse.plugin(`/sessions/${encodeURIComponent(selected)}`, {
          method: "PATCH", body: { mode },
        });
      }, "Сохраняю режим…");
    });
  }
  const delBtn = app.querySelector("[data-delete]");
  if (delBtn) {
    delBtn.addEventListener("click", () => { confirmDelete = true; render(); });
  }
  const delNo = app.querySelector("[data-delete-no]");
  if (delNo) delNo.addEventListener("click", () => { confirmDelete = false; render(); });
  const delYes = app.querySelector("[data-delete-yes]");
  if (delYes) {
    delYes.addEventListener("click", () => act(async () => {
      await pulse.plugin(`/sessions/${encodeURIComponent(selected)}`, { method: "DELETE" });
      selected = "";
      view = null;
      confirmDelete = false;
    }, "Удаляю…"));
  }
  bindLive();
  bindTabs();
  const typeBtn = app.querySelector("[data-type]");
  if (typeBtn) {
    typeBtn.addEventListener("click", () => {
      const box = document.getElementById("type-box");
      if (box.value) sendInput({ kind: "type", text: box.value });
      box.value = "";
    });
  }
  const enterBtn = app.querySelector("[data-enter]");
  if (enterBtn) enterBtn.addEventListener("click", () => sendInput({ kind: "press", key: "Enter" }));
  app.querySelectorAll("[data-confirm]").forEach((el) => {
    el.addEventListener("click", () => {
      const id = el.getAttribute("data-confirm");
      const session = el.getAttribute("data-confirm-session");
      act(async () => {
        await pulse.plugin(`/confirm/${encodeURIComponent(id)}`, {
          method: "POST", body: { session },
        });
      }, "Подтверждаю…");
    });
  });
}

function framePoint(ev) {
  const img = document.getElementById("frame");
  const rect = img.getBoundingClientRect();
  const w = Number(img.dataset.w) || 1280;
  const h = Number(img.dataset.h) || 800;
  return { x: (ev.clientX - rect.left) * (w / rect.width), y: (ev.clientY - rect.top) * (h / rect.height) };
}

function bindLive() {
  const wrap = document.getElementById("view-wrap");
  const img = document.getElementById("frame");
  if (!wrap || !img) return;
  let lastMove = 0;
  img.addEventListener("mousedown", (ev) => {
    ev.preventDefault();
    wrap.focus();
    const btn = ev.button === 2 ? "right" : ev.button === 1 ? "middle" : "left";
    sendInput({ kind: "mousedown", button: btn, ...framePoint(ev) });
  });
  img.addEventListener("mouseup", (ev) => {
    const btn = ev.button === 2 ? "right" : ev.button === 1 ? "middle" : "left";
    sendInput({ kind: "mouseup", button: btn, ...framePoint(ev) });
  });
  img.addEventListener("contextmenu", (ev) => ev.preventDefault());
  img.addEventListener("mousemove", (ev) => {
    const now = Date.now();
    if (now - lastMove < 150) return;
    lastMove = now;
    sendInput({ kind: "move", ...framePoint(ev) });
  });
  let wheel = null;
  wrap.addEventListener("wheel", (ev) => {
    ev.preventDefault();
    const p = framePoint(ev);
    if (wheel) { wheel.dx += ev.deltaX; wheel.dy += ev.deltaY; return; }
    wheel = { dx: ev.deltaX, dy: ev.deltaY };
    setTimeout(() => { sendInput({ kind: "wheel", ...p, ...wheel }); wheel = null; }, 60);
  }, { passive: false });
  wrap.addEventListener("keydown", (ev) => {
    if (ev.ctrlKey || ev.metaKey) return;      // вставка — через событие paste
    if (ev.key.length > 1 || ev.key === " " || ev.key.length === 1) {
      if (["Shift", "Control", "Alt", "Meta", "CapsLock"].includes(ev.key)) return;
      ev.preventDefault();
      sendInput({ kind: "press", key: ev.key, modifiers: (ev.altKey ? 1 : 0) | (ev.shiftKey && ev.key.length > 1 ? 8 : 0) });
    }
  });
  wrap.addEventListener("paste", (ev) => {
    const text = ev.clipboardData?.getData("text");
    if (text) { ev.preventDefault(); sendInput({ kind: "type", text }); }
  });
}

function bindTabs() {
  app.querySelectorAll("[data-tab]").forEach((el) => {
    el.addEventListener("click", async (ev) => {
      if (ev.target.closest("[data-tab-close]")) return;
      try {
        const r = await pulse.plugin(`/sessions/${q(selected)}/tabs/select`, { method: "POST", body: { tab: el.getAttribute("data-tab") } });
        tabs = r.tabs; lastFrame = null; paintTabs(); startPoll();
      } catch (e) { error = e.message; render(); }
    });
  });
  app.querySelectorAll("[data-tab-close]").forEach((el) => {
    el.addEventListener("click", async () => {
      try {
        const r = await pulse.plugin(`/sessions/${q(selected)}/tabs/close`, { method: "POST", body: { tab: el.getAttribute("data-tab-close") } });
        tabs = r.tabs; lastFrame = null; paintTabs(); startPoll();
      } catch (e) { error = e.message; render(); }
    });
  });
  const add = app.querySelector("[data-tab-new]");
  if (add) {
    add.addEventListener("click", async () => {
      try {
        const r = await pulse.plugin(`/sessions/${q(selected)}/tabs/new`, { method: "POST", body: {} });
        tabs = r.tabs; lastFrame = null; paintTabs(); startPoll();
      } catch (e) { error = e.message; render(); }
    });
  }
}

load().then(startPoll);
