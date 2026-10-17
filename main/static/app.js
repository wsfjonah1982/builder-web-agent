// Porto Agent page: a project's Chat tab (adapted from Task Bot's app.js -
// loads the project's Managed Agent session history, sends messages, and
// polls for the agent's events until the session goes idle), Brainstorm
// tab (the project's brainstorm page in an iframe; Send for Analysis copies
// the idea selected there into the project's design folder on TOS) and
// Design tab (every image in that folder).
(function () {
  "use strict";

  var POLL_MS = 2000;
  // If our own user.message never shows up in the event history (should not
  // happen), stop waiting once the session reports idle after this long.
  var ECHO_GRACE_MS = 60000;

  var config = window.PORTO || {};

  // ---------- Project / session / tabs ----------
  // Project and session live in the page URL (?project=&session=), the
  // current tab in its hash (#chat / #brainstorm / #design).
  function reloadWith(params) {
    var query = new URLSearchParams(location.search);
    Object.keys(params).forEach(function (k) {
      if (params[k]) query.set(k, params[k]);
      else query.delete(k);
    });
    location.search = query.toString() + location.hash;
  }

  document.getElementById("projectForm").addEventListener("submit", function (e) {
    e.preventDefault();
    var id = document.getElementById("projectInput").value.trim();
    if (id) reloadWith({ project: id, session: "" });
  });

  document.getElementById("sessionForm").addEventListener("submit", function (e) {
    e.preventDefault();
    var sid = document.getElementById("sessionInput").value.trim();
    if (sid && sid !== config.session) reloadWith({ project: config.project, session: sid });
  });

  var tabs = document.querySelectorAll(".tab");
  var frame = document.getElementById("brainstormFrame");

  // The brainstorm page is served from this site (/brainstorm/<project>),
  // not straight from TOS, so its DOM and localStorage can be read here.
  function loadBrainstorm(force) {
    if (!frame || !config.brainstormPage) return;
    if (force || !frame.getAttribute("src")) {
      frame.src = config.brainstormPage + (force ? "?t=" + Date.now() : "");
    }
  }

  function showTab(name) {
    if (name !== "brainstorm" && name !== "design") name = "chat";
    tabs.forEach(function (tab) {
      var on = tab.dataset.tab === name;
      tab.classList.toggle("active", on);
      tab.setAttribute("aria-selected", on ? "true" : "false");
      document.getElementById("panel-" + tab.dataset.tab).hidden = !on;
    });
    if (name === "brainstorm") loadBrainstorm(false);
    else if (name === "design") { if (!designLoaded) loadDesign(); }
    else if (!textInput.disabled) textInput.focus();
  }

  tabs.forEach(function (tab) {
    tab.addEventListener("click", function () {
      history.replaceState(null, "", "#" + tab.dataset.tab);
      showTab(tab.dataset.tab);
    });
  });
  window.addEventListener("hashchange", function () { showTab(location.hash.slice(1)); });
  document.getElementById("brainstormReload").addEventListener("click", function () { loadBrainstorm(true); });

  // ---------- Send for Analysis ----------
  // Reads the idea selected on the brainstorm page ("Select for analysis"
  // marks its <article class=row> with .sel; the page also keeps the pick in
  // localStorage["bbd-brainstorm-<project>"]) and copies only that idea's
  // designs into the project's design folder on TOS.
  var sendBtnAnalysis = document.getElementById("sendAnalysis");
  var sendStatus = document.getElementById("sendStatus");

  function setSendStatus(text, kind) {
    sendStatus.textContent = text;
    sendStatus.className = "send-status" + (kind ? " send-" + kind : "");
  }

  function readSelection() {
    var doc;
    try { doc = frame && frame.contentDocument; } catch (e) { doc = null; }
    if (!doc || !doc.querySelector(".row")) return null;
    var row = doc.querySelector(".row.sel");
    if (!row) {
      var st = {};
      try { st = JSON.parse(localStorage.getItem("bbd-brainstorm-" + config.project) || "{}"); } catch (e) { st = {}; }
      if (st.pick) row = doc.querySelector('.row[data-id="' + CSS.escape(String(st.pick)) + '"]');
    }
    if (!row) return { none: true };
    var note = row.querySelector("textarea");
    var general = doc.getElementById("general");
    return {
      id: row.dataset.id,
      title: row.dataset.title || "Idea " + row.dataset.id,
      note: note ? note.value.trim() : "",
      general: general ? general.value.trim() : ""
    };
  }

  sendBtnAnalysis.addEventListener("click", function () {
    var sel = readSelection();
    if (!sel) { setSendStatus("The brainstorm page hasn't loaded yet.", "error"); return; }
    if (sel.none) { setSendStatus("Select an idea on the page first (“Select for analysis”).", "error"); return; }
    sendBtnAnalysis.disabled = true;
    setSendStatus("Copying " + sel.title + " to the design folder...");
    fetch("/api/design/" + encodeURIComponent(config.project) + "/select", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ idea_id: sel.id, note: sel.note, general: sel.general })
    })
      .then(function (r) { return r.json().then(function (d) { if (!r.ok) throw new Error(d.error || "Send failed"); return d; }); })
      .then(function (data) {
        sendBtnAnalysis.disabled = false;
        sendStatus.textContent = "";
        sendStatus.className = "send-status send-ok";
        var created = data.groups.filter(function (g) { return g.folder === data.created; })[0];
        sendStatus.appendChild(document.createTextNode("Sent " + sel.title + ": " +
          (created ? created.images.length : 0) + " designs in design/" + data.created + "/. "));
        var link = document.createElement("a");
        link.href = "#design";
        link.textContent = "View";
        sendStatus.appendChild(link);
        if (designGroups) {
          designLoaded = true;  // the response is the new folder listing
          renderFolder(data);
        }
      })
      .catch(function (err) {
        sendBtnAnalysis.disabled = false;
        setSendStatus(err.message || "Send failed", "error");
      });
  });

  // ---------- Chat ----------
  var messagesEl = document.getElementById("messages");
  var innerEl = document.getElementById("messagesInner");
  var emptyEl = document.getElementById("emptyState");
  var loadingEl = document.getElementById("loadingState");
  var form = document.getElementById("composer");
  var textInput = document.getElementById("textInput");
  var sendBtn = document.getElementById("sendBtn");
  var attachBtn = document.getElementById("attachBtn");
  var fileInput = document.getElementById("fileInput");
  var attachmentsEl = document.getElementById("attachments");

  var seen = new Set();
  var cursor = null;
  var pendingFiles = [];
  var sending = false;
  var pollTimer = null;
  // The turn we're waiting on: our user.message's event id, its server
  // timestamp once seen in the history, and when we sent it.
  var waiting = null;
  var typingEl = null;

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function nearBottom() {
    return messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < 120;
  }

  function scrollToBottom(force) {
    if (force || nearBottom()) messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  // Every API call carries the page's session id.
  function apiUrl(path, params) {
    var query = new URLSearchParams(params || {});
    if (config.session) query.set("session", config.session);
    var qs = query.toString();
    return path + (qs ? "?" + qs : "");
  }

  function appendNode(node) {
    emptyEl.hidden = true;
    if (typingEl) innerEl.insertBefore(node, typingEl);
    else innerEl.appendChild(node);
  }

  function renderUser(text, images, attachments) {
    var row = el("div", "msg msg-user");
    var bubble = el("div", "bubble");
    if (images && images.length) {
      var imgs = el("div", "msg-images");
      images.forEach(function (src) {
        var img = el("img");
        img.src = src;
        img.alt = "attached image";
        img.loading = "lazy";
        imgs.appendChild(img);
      });
      bubble.appendChild(imgs);
    }
    if (attachments && attachments.length) {
      var files = el("div", "msg-files");
      attachments.forEach(function (name) { files.appendChild(el("span", "file-tag", name)); });
      bubble.appendChild(files);
    }
    if (text) bubble.appendChild(document.createTextNode(text));
    row.appendChild(bubble);
    appendNode(row);
  }

  function renderAssistant(item) {
    var row = el("div", "msg msg-assistant");
    var bubble = el("div", "bubble");
    var body = el("div", "md");
    // Server-rendered Markdown, HTML-escaped first.
    body.innerHTML = item.html;
    bubble.appendChild(body);
    bubble.querySelectorAll("a[href]").forEach(function (a) {
      // Markdown links aren't URL-sanitized server-side; drop javascript: etc.
      if (!/^(https?:|mailto:)/i.test(a.getAttribute("href"))) a.removeAttribute("href");
      a.target = "_blank";
      a.rel = "noopener noreferrer";
    });
    row.appendChild(bubble);
    appendNode(row);
  }

  function renderError(text) {
    var row = el("div", "msg msg-error");
    row.appendChild(el("div", "bubble", text));
    appendNode(row);
  }

  function showTyping(on) {
    if (on && !typingEl) {
      typingEl = el("div", "typing");
      var dots = el("span", "dots");
      dots.innerHTML = "<i></i><i></i><i></i>";
      typingEl.appendChild(dots);
      typingEl.appendChild(document.createTextNode("Working on it..."));
      innerEl.appendChild(typingEl);
      scrollToBottom(true);
    } else if (!on && typingEl) {
      typingEl.remove();
      typingEl = null;
    }
  }

  // Returns true if the item ends the turn we're waiting on.
  function handleItem(item) {
    if (item.id && seen.has(item.id)) return false;
    if (item.id) seen.add(item.id);

    if (item.kind === "status") {
      return !!(waiting && waiting.userTs != null && item.ts >= waiting.userTs &&
        (item.status === "idle" || item.status === "terminated"));
    }
    if (item.kind === "user") renderUser(item.text, item.images, item.attachments);
    else if (item.kind === "assistant") renderAssistant(item);
    else if (item.kind === "error") renderError(item.text);
    return false;
  }

  function applyPayload(data) {
    var stick = nearBottom();
    var finished = false;
    (data.items || []).forEach(function (item) {
      // Our own message was already rendered optimistically; just note when
      // the server recorded it, so later idle events can be matched to it.
      if (waiting && item.id === waiting.eventId && waiting.userTs == null) waiting.userTs = item.ts;
      if (handleItem(item)) finished = true;
    });
    if (data.cursor) cursor = data.cursor;

    var status = data.status;
    if (waiting && !finished && (status === "idle" || status === "terminated") &&
        Date.now() - waiting.sentAt > ECHO_GRACE_MS) {
      finished = true;
    }
    if (finished) waiting = null;

    var busy = !!waiting || status === "running";
    showTyping(busy);
    updateSendState();
    if (stick) scrollToBottom(true);
    if (busy) schedulePoll();
    else stopPolling();
  }

  function schedulePoll() {
    if (pollTimer) return;
    pollTimer = setTimeout(function () {
      pollTimer = null;
      poll();
    }, POLL_MS);
  }

  function stopPolling() {
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = null;
  }

  function poll() {
    if (sending) { schedulePoll(); return; }
    if (!cursor) { loadHistory(); return; }
    fetch(apiUrl("/api/events", { since: cursor }))
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); })
      .then(function (res) {
        if (!res.ok) throw new Error(res.data.error || "Update failed");
        applyPayload(res.data);
      })
      .catch(function () { schedulePoll(); });
  }

  function loadHistory() {
    fetch(apiUrl("/api/history"))
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); })
      .then(function (res) {
        loadingEl.remove();
        if (!res.ok) {
          renderError(res.data.error || "Could not load the conversation.");
          return;
        }
        applyPayload(res.data);
        if (!innerEl.querySelector(".msg")) emptyEl.hidden = false;
        scrollToBottom(true);
      })
      .catch(function () {
        loadingEl.remove();
        renderError("Could not reach the server.");
      });
  }

  // ---------- Composer ----------
  function autosize() {
    textInput.style.height = "auto";
    textInput.style.height = Math.min(textInput.scrollHeight, 200) + "px";
  }

  function renderAttachments() {
    attachmentsEl.innerHTML = "";
    attachmentsEl.hidden = pendingFiles.length === 0;
    pendingFiles.forEach(function (file, i) {
      var chip = el("span", "attachment");
      chip.appendChild(el("span", null, file.name));
      var remove = el("button", null, "×");
      remove.type = "button";
      remove.title = "Remove";
      remove.addEventListener("click", function () {
        pendingFiles.splice(i, 1);
        renderAttachments();
      });
      chip.appendChild(remove);
      attachmentsEl.appendChild(chip);
    });
  }

  function updateSendState() {
    sendBtn.disabled = sending || !!waiting;
  }

  function send() {
    var text = textInput.value.trim();
    var files = pendingFiles;
    if ((!text && !files.length) || sending || waiting) return;

    var body = new FormData();
    body.append("text", text);
    files.forEach(function (f) { body.append("files", f); });

    var images = files.filter(function (f) { return /^image\//.test(f.type); })
      .map(function (f) { return URL.createObjectURL(f); });
    var otherNames = files.filter(function (f) { return !/^image\//.test(f.type); })
      .map(function (f) { return f.name; });

    renderUser(text, images, otherNames);
    scrollToBottom(true);
    var draft = { text: textInput.value, files: files.slice() };
    textInput.value = "";
    pendingFiles = [];
    renderAttachments();
    autosize();

    sending = true;
    updateSendState();
    showTyping(true);

    fetch(apiUrl("/api/message"), { method: "POST", body: body })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); })
      .then(function (res) {
        sending = false;
        if (!res.ok) throw new Error(res.data.error || "Send failed");
        if (res.data.event_id) seen.add(res.data.event_id);
        waiting = { eventId: res.data.event_id, userTs: null, sentAt: Date.now() };
        updateSendState();
        schedulePoll();
      })
      .catch(function (err) {
        sending = false;
        showTyping(false);
        updateSendState();
        // Restore the draft so nothing typed is lost.
        textInput.value = draft.text;
        pendingFiles = draft.files;
        renderAttachments();
        autosize();
        renderError(err.message || "Send failed");
        scrollToBottom(true);
      });
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    send();
  });

  textInput.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      send();
    }
  });
  textInput.addEventListener("input", autosize);

  attachBtn.addEventListener("click", function () { fileInput.click(); });
  fileInput.addEventListener("change", function () {
    Array.prototype.forEach.call(fileInput.files, function (f) { pendingFiles.push(f); });
    fileInput.value = "";
    renderAttachments();
  });

  // ---------- Design ----------
  var designGroups = document.getElementById("designGroups");
  var designFolder = document.getElementById("designFolder");
  var designRefresh = document.getElementById("designRefresh");
  var zoom = document.getElementById("zoom");
  var zoomImg = document.getElementById("zoomImg");
  var designLoaded = false;

  function designApi(path) {
    return "/api/design/" + encodeURIComponent(config.project) + (path || "");
  }

  function getJson(url, opts) {
    return fetch(url, opts).then(function (r) {
      return r.json().then(function (d) {
        if (!r.ok) throw new Error(d.error || "Request failed");
        return d;
      });
    });
  }

  function openZoom(src, caption) {
    zoomImg.src = src;
    zoomImg.alt = caption;
    document.getElementById("zoomCap").textContent = caption;
    zoom.showModal();
  }

  function figure(src, caption, sub) {
    var fig = el("figure", "design-fig");
    var btn = el("button", "design-frame");
    btn.type = "button";
    btn.title = "View full size";
    var img = el("img");
    img.src = src;
    img.alt = caption;
    img.loading = "lazy";
    btn.appendChild(img);
    btn.addEventListener("click", function () { openZoom(src, caption); });
    fig.appendChild(btn);
    var cap = el("figcaption");
    var text = el("div", "design-cap-text");
    text.appendChild(el("b", null, caption));
    if (sub) text.appendChild(el("span", null, sub));
    cap.appendChild(text);
    fig.appendChild(cap);
    return fig;
  }

  function designFigure(img) {
    var label = img.label || img.name;
    var sub = img.name + (img.edited_at ? " · edited " + new Date(img.edited_at).toLocaleString() : "");
    var fig = figure(img.url, label, sub);
    if (window.DesignEditor) {
      var edit = el("button", "btn-small btn-ghost design-edit", "Edit");
      edit.type = "button";
      edit.title = "Edit " + label + " (draw, box, text, Seedream)";
      edit.addEventListener("click", function () {
        window.DesignEditor.open({ project: config.project, name: img.name, label: label, onSaved: renderFolder });
      });
      fig.querySelector("figcaption").appendChild(edit);
    }
    return fig;
  }

  // One section per Send for Analysis (timestamp folder), newest first.
  function designGroup(group, latest) {
    var section = el("section", "design-group");
    var head = el("div", "design-head");
    var sel = group.selection;
    var title = sel && sel.idea ? sel.idea.title : group.folder ? "Untitled pick" : "Earlier pick";
    head.appendChild(el("h2", null, title));
    if (latest) head.appendChild(el("span", "design-badge", "Latest"));
    var meta = [];
    if (sel && sel.selected_at) meta.push("selected " + new Date(sel.selected_at).toLocaleString());
    if (!sel) meta.push("no selection.json");
    head.appendChild(el("span", "design-sel", meta.join(" · ")));
    var where = el("span", "design-path", group.folder ? "design/" + group.folder + "/" : "design/ (top level)");
    where.title = group.url;
    head.appendChild(where);
    section.appendChild(head);
    var grid = el("div", "design-grid");
    group.images.forEach(function (img) { grid.appendChild(designFigure(img)); });
    section.appendChild(grid);
    return section;
  }

  function renderFolder(data) {
    designFolder.textContent = data.folder;
    designFolder.title = data.folder;
    designGroups.innerHTML = "";
    if (!data.groups.length) {
      designGroups.appendChild(el("p", "design-empty",
        "The design folder is empty. Select an idea on the Brainstorm tab and click Send for Analysis."));
    }
    data.groups.forEach(function (group, i) {
      designGroups.appendChild(designGroup(group, i === 0));
    });
  }

  function loadDesign() {
    if (!designGroups || !config.project || config.error) return;
    designLoaded = true;
    getJson(designApi()).then(renderFolder).catch(function (err) {
      designGroups.innerHTML = "";
      designGroups.appendChild(el("p", "design-empty design-error", err.message));
    });
  }

  designRefresh.addEventListener("click", loadDesign);
  document.getElementById("zoomClose").addEventListener("click", function () { zoom.close(); });
  zoom.addEventListener("click", function (e) { if (e.target === zoom || e.target === zoomImg) zoom.close(); });
  zoom.addEventListener("close", function () { zoomImg.removeAttribute("src"); });

  function disableChat(message) {
    loadingEl.remove();
    renderError(message);
    textInput.disabled = true;
    sendBtn.disabled = true;
    attachBtn.disabled = true;
  }

  if (config.error) disableChat(config.error);
  else if (!config.session) disableChat("No session for this project. Enter a session id above and press Connect.");
  else loadHistory();

  showTab(location.hash.slice(1));
})();
