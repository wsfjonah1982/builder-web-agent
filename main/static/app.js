// Porto Agent page: a project's Chat tab (adapted from Task Bot's app.js -
// loads the project's Managed Agent session history, sends messages, polls
// for the agent's events until the session goes idle, and shows the guard's
// reply categories and result buttons), Brainstorm tab (the project's
// brainstorm page in an iframe; Send for Analysis copies the idea selected
// there into the project's design folder on TOS), Design tab (every image
// in that folder, the editor, and Request Analysis Report) and Analysis tab
// (the project's analysis page in an iframe).
(function () {
  "use strict";

  var POLL_MS = 2000;
  // If our own user.message never shows up in the event history (should not
  // happen), stop waiting once the session reports idle after this long.
  var ECHO_GRACE_MS = 60000;

  var config = window.PORTO || {};

  // ---------- Project / session / tabs ----------
  // Project and session live in the page URL (?project=&session=), the
  // current tab in its hash (#chat / #brainstorm / #design / #analysis).
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
  var analysisFrame = document.getElementById("analysisFrame");

  // The brainstorm and analysis pages are served from this site
  // (/brainstorm/<project>, /analysis/<project>), not straight from TOS, so
  // their DOM and localStorage can be read here.
  function loadFrame(target, page, force) {
    if (!target || !page) return;
    if (force || !target.getAttribute("src")) {
      target.src = page + (force ? "?t=" + Date.now() : "");
    }
  }
  function loadBrainstorm(force) { loadFrame(frame, config.brainstormPage, force); }
  function loadAnalysis(force) { loadFrame(analysisFrame, config.analysisPage, force); }

  function showTab(name) {
    if (["brainstorm", "design", "analysis"].indexOf(name) === -1) name = "chat";
    tabs.forEach(function (tab) {
      var on = tab.dataset.tab === name;
      tab.classList.toggle("active", on);
      tab.setAttribute("aria-selected", on ? "true" : "false");
      document.getElementById("panel-" + tab.dataset.tab).hidden = !on;
    });
    if (name === "brainstorm") loadBrainstorm(false);
    else if (name === "analysis") loadAnalysis(false);
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
  document.getElementById("analysisReload").addEventListener("click", function () { loadAnalysis(true); });

  // Opens a tab from a reply's result button, reloading its page (the agent
  // has just written it).
  function openTab(name) {
    history.replaceState(null, "", "#" + name);
    showTab(name);
    if (name === "brainstorm") loadBrainstorm(true);
    else if (name === "analysis") loadAnalysis(true);
  }

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

  // The "[Project id: ...]" block the server puts before a message until
  // the session has been told the project id.
  function addProjectTag(bubble, project) {
    var tag = el("span", "file-tag project-tag", "Project: " + project);
    tag.title = "The project id was sent to the agent with this message";
    var files = bubble.querySelector(".msg-files");
    if (!files) {
      files = el("div", "msg-files");
      bubble.insertBefore(files, bubble.firstChild);
    }
    files.insertBefore(tag, files.firstChild);
  }

  function renderUser(text, images, attachments, project) {
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
    if (project) addProjectTag(bubble, project);
    row.appendChild(bubble);
    appendNode(row);
    return bubble;
  }

  // Page wording from ui_text.md (server-side), with built-in fallbacks.
  var TEXT = config.text || {};
  function t(key, fallback) { return TEXT[key] || fallback; }

  // Hover hints for the reply categories from the response guardrail.
  function categoryHint(category) {
    return t("hint_" + category.toLowerCase(), "");
  }

  // A "Go" button only makes sense on the newest reply, before anything else
  // has been sent.
  function retireGoButtons() {
    innerEl.querySelectorAll(".btn-go").forEach(function (b) {
      b.disabled = true;
      b.dataset.retired = "1";
    });
  }

  // A result link ending in brainstorm.html / analysis.html gets "View
  // Brainstorm" / "View Analysis", which opens that tab when it's this
  // project's page (another project's opens in a new browser tab); any
  // other link gets "View Output" in a new tab. The guard only passes
  // unsigned result links on the bucket.
  var PAGE_TABS = [
    { tab: "brainstorm", file: "brainstorm.html", key: "view_brainstorm_button", label: "View Brainstorm" },
    { tab: "analysis", file: "analysis.html", key: "view_analysis_button", label: "View Analysis" },
  ];

  function outputButton(url) {
    var path = url.split(/[?#]/)[0];
    var file = path.split("/").pop();
    var page = PAGE_TABS.filter(function (p) { return p.file === file; })[0];
    var btn = el("a", "btn-action btn-view",
                 page ? t(page.key, page.label) : t("view_output_button", "View Output"));
    btn.href = url;
    btn.title = url;
    if (page && config[page.tab] && path === config[page.tab]) {
      btn.addEventListener("click", function (e) {
        if (e.ctrlKey || e.metaKey || e.shiftKey || e.button !== 0) return;
        e.preventDefault();
        openTab(page.tab);
      });
    }
    return btn;
  }

  function renderAssistant(item) {
    var category = item.category || "";
    retireGoButtons();
    var row = el("div", "msg msg-assistant" + (category ? " cat-" + category.toLowerCase() : ""));
    var bubble = el("div", "bubble");

    if (category) {
      var head = el("div", "reply-label");
      var chip = el("span", "cat-chip", category);
      chip.title = categoryHint(category);
      head.appendChild(chip);
      (item.also || []).forEach(function (extra) {
        var sub = el("span", "cat-chip cat-chip-sub cat-" + extra.toLowerCase(), extra);
        sub.title = categoryHint(extra);
        head.appendChild(sub);
      });
      bubble.appendChild(head);
    }

    var body = el("div", "md");
    // Server-rendered Markdown of the guard's rewrite, HTML-escaped first.
    body.innerHTML = item.html;
    bubble.appendChild(body);

    var isGate = category === "Gate" || (item.also || []).indexOf("Gate") !== -1;
    if (item.output_url || isGate) {
      var actions = el("div", "reply-actions");
      if (item.output_url) actions.appendChild(outputButton(item.output_url));
      if (isGate) {
        var go = el("button", "btn-action btn-go", t("go_button", "Go"));
        go.type = "button";
        go.disabled = sending || !!waiting;
        go.addEventListener("click", function () {
          if (!go.disabled) sendText(t("go_reply", "Go"));
        });
        actions.appendChild(go);
      }
      bubble.appendChild(actions);
    }
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
    if (item.kind === "user") renderUser(item.text, item.images, item.attachments, item.project);
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
    var lastGo = innerEl.querySelectorAll(".btn-go");
    lastGo = lastGo[lastGo.length - 1];
    if (lastGo && lastGo.dataset.retired !== "1") lastGo.disabled = sending || !!waiting;
  }

  function send() {
    sendText(textInput.value.trim(), true);
  }

  // The agent can take a message (nothing being sent or worked on).
  function chatReady() {
    return !!config.session && !config.error && !sending && !waiting;
  }

  // Sends `text` (from the composer, with any pending attachments, or a
  // quick reply such as the Go button's "Go", which leaves the composer as is).
  // onSent runs once the agent has accepted the message, onFailed if not.
  function sendText(text, fromComposer, onSent, onFailed) {
    var files = fromComposer ? pendingFiles : [];
    if ((!text && !files.length) || !chatReady()) return;

    var body = new FormData();
    body.append("text", text);
    // The server puts it before the message until the session has had it.
    if (config.project) body.append("project", config.project);
    files.forEach(function (f) { body.append("files", f); });

    var images = files.filter(function (f) { return /^image\//.test(f.type); })
      .map(function (f) { return URL.createObjectURL(f); });
    var otherNames = files.filter(function (f) { return !/^image\//.test(f.type); })
      .map(function (f) { return f.name; });

    var sentBubble = renderUser(text, images, otherNames);
    scrollToBottom(true);
    var draft = fromComposer ? { text: textInput.value, files: files.slice() } : null;
    if (fromComposer) {
      textInput.value = "";
      pendingFiles = [];
      renderAttachments();
      autosize();
    }

    sending = true;
    updateSendState();
    showTyping(true);

    fetch(apiUrl("/api/message"), { method: "POST", body: body })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); })
      .then(function (res) {
        sending = false;
        if (!res.ok) throw new Error(res.data.error || "Send failed");
        if (res.data.event_id) seen.add(res.data.event_id);
        if (res.data.project) addProjectTag(sentBubble, res.data.project);
        waiting = { eventId: res.data.event_id, userTs: null, sentAt: Date.now() };
        updateSendState();
        schedulePoll();
        if (onSent) setTimeout(onSent, 0);  // outside this chain: its errors aren't send failures
      })
      .catch(function (err) {
        sending = false;
        showTyping(false);
        updateSendState();
        // Restore the draft so nothing typed is lost.
        if (draft) {
          textInput.value = draft.text;
          pendingFiles = draft.files;
          renderAttachments();
          autosize();
        }
        renderError(err.message || "Send failed");
        scrollToBottom(true);
        if (onFailed) onFailed(err);
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
    if (sel && sel.analysis_requested_at) {
      meta.push("analysis requested " + new Date(sel.analysis_requested_at).toLocaleString());
    }
    if (!sel) meta.push("no selection.json");
    head.appendChild(el("span", "design-sel", meta.join(" · ")));
    var where = el("span", "design-path", group.folder ? "design/" + group.folder + "/" : "design/ (top level)");
    where.title = group.url;
    head.appendChild(where);
    section.appendChild(head);
    var grid = el("div", "design-grid");
    group.images.forEach(function (img) { grid.appendChild(designFigure(img)); });
    section.appendChild(grid);
    // Timestamp folders with a selection.json can go to the agent for the
    // analysis report (the older top-level layout can't be marked).
    if (group.folder && sel && sel.idea) section.appendChild(analysisRequestBar(group));
    return section;
  }

  // The message for the analysis report: the request (ui_text.md
  // "analysis_request"), then the pick in the brainstorm page's
  // brand-design-selection format, plus where its images are now.
  function analysisRequestText(group) {
    var sel = group.selection;
    var payload = {
      type: "brand-design-selection", version: 1, request: "analysis-report",
      project_id: config.project, page: "design", date: new Date().toISOString(),
      selected: { id: sel.idea.id, title: sel.idea.title, note: sel.note || "" },
      general: sel.general || "",
      folder: "design/" + group.folder + "/", folder_url: group.url,
      designs: group.images.map(function (img) {
        return { label: img.label || img.file, url: img.url.split("?")[0], edited: !!img.edited_at };
      }),
    };
    return t("analysis_request", "Please write the analysis report for the design selected below.") +
      "\n\n" + JSON.stringify(payload, null, 2);
  }

  function analysisRequestBar(group) {
    var bar = el("div", "design-actions");
    var btn = el("button", "btn-small", t("analysis_report_button", "Request Analysis Report"));
    btn.type = "button";
    var status = el("span", "send-status");
    status.setAttribute("role", "status");
    btn.addEventListener("click", function () {
      if (!chatReady()) {
        status.textContent = config.session ? "The agent is still working. Try again when it has finished."
                                            : "This project has no chat session.";
        status.className = "send-status send-error";
        return;
      }
      var title = group.selection.idea.title;
      if (!confirm('Send "' + title + '" (design/' + group.folder + '/) to the agent for the analysis report?')) return;
      btn.disabled = true;
      status.textContent = "Sending...";
      status.className = "send-status";
      openTab("chat");
      sendText(analysisRequestText(group), false, function () {
        getJson(designApi("/analysis-requested"), {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ folder: group.folder }),
        }).then(renderFolder).catch(function (err) {
          btn.disabled = false;
          status.textContent = "Sent, but the request time couldn't be saved: " + err.message;
          status.className = "send-status send-error";
        });
      }, function (err) {
        btn.disabled = false;
        status.textContent = "Not sent: " + (err.message || "send failed");
        status.className = "send-status send-error";
      });
    });
    bar.appendChild(btn);
    bar.appendChild(status);
    return bar;
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
