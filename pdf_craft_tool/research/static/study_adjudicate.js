/* Blinded 5-voter study adjudication UI (plain JS, no CDN).
 *
 * Keyboard-first flow: the region crop is shown above an editable
 * "resolved text" box prefilled with the most common candidate reading;
 * 1-5 / A-E pick a candidate, Ctrl+Enter (or Enter outside text fields)
 * submits and auto-advances to the next open conflict, across pages.
 * Candidates are anonymous labels A-E only; no identities appear here.
 * Requests carry the adjudicator token in the X-Annotation-Token header
 * (crops too: they are fetched as blobs, not plain <img src>).
 */
(function () {
  "use strict";

  var TOKEN_KEY = "annot.token";
  var SIZE_KEY = "study.textSize";
  var TOKEN_HEADER = "X-Annotation-Token";
  var TEXT_SIZES = [1.05, 1.2, 1.35, 1.5, 1.7, 1.95];
  var MAX_DIFF_CELLS = 2500000;
  var MAX_CACHED_CROPS = 24;

  function $(id) { return document.getElementById(id); }
  var el = {
    login: $("login"), loginForm: $("login-form"), token: $("token"),
    loginError: $("login-error"), workspace: $("workspace"), done: $("done"),
    doneDetail: $("done-detail"), actionbar: $("actionbar"),
    crop: $("crop"), cropFrame: $("crop-frame"), cropLoading: $("crop-loading"),
    regionLabel: $("region-label"), pageLabel: $("page-label"),
    resolved: $("resolved"), choiceState: $("choice-state"),
    candidates: $("candidates"), reason: $("reason"),
    submit: $("submit-btn"), skip: $("skip-btn"), prev: $("prev-btn"),
    emptyBtn: $("empty-btn"),
    pageSelect: $("page-select"), help: $("help"), helpBtn: $("help-btn"),
    tokenBtn: $("token-btn"), zoom: $("zoom"), zoomImg: $("zoom-img"),
    toast: $("toast"),
    pPage: $("p-page"), pPages: $("p-pages"), pItem: $("p-item"),
    pItems: $("p-items"), pDone: $("p-done"), pTotal: $("p-total"),
    pBar: $("p-bar")
  };

  var state = {
    pages: [], summary: null, pageIdx: -1, items: [], itemIdx: 0,
    drafts: {}, busy: false
  };

  // ---- storage / network -------------------------------------------
  function load(key) {
    try { return window.localStorage.getItem(key) || ""; } catch (e) { return ""; }
  }
  function save(key, value) {
    try {
      if (value === null) { window.localStorage.removeItem(key); }
      else { window.localStorage.setItem(key, value); }
    } catch (e) { /* storage unavailable: keep going in memory */ }
  }
  function authHeaders() {
    var headers = {};
    headers[TOKEN_HEADER] = load(TOKEN_KEY);
    return headers;
  }
  function httpError(message, status) {
    var error = new Error(message);
    error.status = status;
    return error;
  }

  async function api(method, path, body) {
    var options = { method: method, headers: authHeaders(), cache: "no-store" };
    if (body !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    var response = await fetch(path, options);
    var data = null;
    try { data = await response.json(); } catch (e) { data = null; }
    if (!response.ok) {
      throw httpError((data && data.error) || ("Request failed (" + response.status + ")"),
                      response.status);
    }
    return data;
  }

  // ---- region crops (blob URLs, small LRU, prefetch) ----------------
  var crops = new Map();
  function cropKey(pageId, regionIndex) { return pageId + "|" + regionIndex; }
  function cropUrl(pageId, regionIndex) {
    var key = cropKey(pageId, regionIndex);
    if (crops.has(key)) {
      var hit = crops.get(key);
      crops.delete(key);
      crops.set(key, hit);  // refresh LRU order
      return hit;
    }
    var promise = fetch("/api/study/crop/" + encodeURIComponent(pageId) + "/" + regionIndex,
                        { headers: authHeaders(), cache: "no-store" })
      .then(function (response) {
        if (!response.ok) { throw httpError("Region image unavailable (" + response.status + ")", response.status); }
        return response.blob();
      })
      .then(function (blob) { return URL.createObjectURL(blob); });
    promise.catch(function () { crops.delete(key); });
    crops.set(key, promise);
    while (crops.size > MAX_CACHED_CROPS) {
      var oldest = crops.keys().next().value;
      var stale = crops.get(oldest);
      crops.delete(oldest);
      stale.then(function (url) { setTimeout(function () { URL.revokeObjectURL(url); }, 5000); },
                 function () {});
    }
    return promise;
  }

  var cropSeq = 0;
  function showCrop(pageId, regionIndex) {
    var seq = ++cropSeq;
    el.cropFrame.classList.remove("is-failed");
    el.cropFrame.classList.add("is-loading");
    el.cropLoading.textContent = "Loading image…";
    cropUrl(pageId, regionIndex).then(function (url) {
      if (seq !== cropSeq) { return; }
      var ready = function () {
        if (seq !== cropSeq) { return; }
        var w = el.crop.naturalWidth || 1, h = el.crop.naturalHeight || 1;
        el.workspace.classList.toggle("is-tall", w / h < 2.2);
        el.crop.style.maxWidth = Math.round(w * 3) + "px";
        el.cropFrame.classList.remove("is-loading");
      };
      el.crop.onload = ready;
      if (el.crop.src === url && el.crop.complete) { ready(); }
      else { el.crop.src = url; }
      el.zoomImg.src = url;
    }, function (error) {
      if (seq !== cropSeq) { return; }
      el.cropFrame.classList.remove("is-loading");
      el.cropFrame.classList.add("is-failed");
      el.crop.removeAttribute("src");
      el.cropLoading.textContent = error.message || "Region image unavailable";
    });
  }

  // ---- text helpers --------------------------------------------------
  var segmenter = (typeof Intl !== "undefined" && Intl.Segmenter)
    ? new Intl.Segmenter("bn", { granularity: "grapheme" }) : null;
  function graphemes(text) {
    if (!segmenter) { return Array.from(text); }
    var out = [];
    for (var part of segmenter.segment(text)) { out.push(part.segment); }
    return out;
  }
  function norm(text) {
    return (text || "").normalize("NFC").replace(/\s+/g, " ").trim();
  }

  /* Grapheme LCS diff of `cand` against `ref`: [kind, text] runs where kind
   * is "same", "ins" (only in cand) or "del" (only in ref). */
  function diff(ref, cand) {
    var a = graphemes(ref), b = graphemes(cand);
    var ops = [];
    function push(kind, text) {
      var last = ops[ops.length - 1];
      if (last && last[0] === kind) { last[1] += text; } else { ops.push([kind, text]); }
    }
    var pre = 0;
    while (pre < a.length && pre < b.length && a[pre] === b[pre]) { pre++; }
    var suf = 0;
    while (suf < a.length - pre && suf < b.length - pre
           && a[a.length - 1 - suf] === b[b.length - 1 - suf]) { suf++; }
    var A = a.slice(pre, a.length - suf), B = b.slice(pre, b.length - suf);
    var n = A.length, m = B.length;
    if (pre) { push("same", b.slice(0, pre).join("")); }
    if (n * m > MAX_DIFF_CELLS) {
      if (n) { push("del", A.join("")); }
      if (m) { push("ins", B.join("")); }
    } else {
      var w = m + 1, table = new Uint32Array((n + 1) * w);
      for (var i = n - 1; i >= 0; i--) {
        for (var j = m - 1; j >= 0; j--) {
          table[i * w + j] = A[i] === B[j]
            ? table[(i + 1) * w + j + 1] + 1
            : Math.max(table[(i + 1) * w + j], table[i * w + j + 1]);
        }
      }
      var x = 0, y = 0;
      while (x < n && y < m) {
        if (A[x] === B[y]) { push("same", B[y]); x++; y++; }
        else if (table[(x + 1) * w + y] >= table[x * w + y + 1]) { push("del", A[x]); x++; }
        else { push("ins", B[y]); y++; }
      }
      while (x < n) { push("del", A[x++]); }
      while (y < m) { push("ins", B[y++]); }
    }
    if (suf) { push("same", b.slice(b.length - suf).join("")); }
    return ops;
  }

  // ---- item / draft model -------------------------------------------
  function currentPage() { return state.pages[state.pageIdx] || null; }
  function currentItem() { return state.items[state.itemIdx] || null; }
  function okCandidates(item) {
    return item.candidates.filter(function (c) { return c.status === "ok"; });
  }
  function byLabel(item, label) {
    for (var i = 0; i < item.candidates.length; i++) {
      if (item.candidates[i].label === label) { return item.candidates[i]; }
    }
    return null;
  }
  function agreement(item) {
    var counts = {};
    okCandidates(item).forEach(function (c) {
      var key = norm(c.text);
      counts[key] = (counts[key] || 0) + 1;
    });
    return counts;
  }
  function bengaliRatio(value) {
    var letters = 0, bengali = 0;
    for (var ch of value) {
      if (/[\p{L}\p{M}]/u.test(ch)) {
        letters++;
        if (ch >= "ঀ" && ch <= "৿") { bengali++; }
      }
    }
    return letters ? bengali / letters : 0;
  }
  function lcsLength(a, b) {
    if (a.length * b.length > MAX_DIFF_CELLS) { return 0; }
    var prev = new Uint32Array(b.length + 1), cur = new Uint32Array(b.length + 1);
    for (var i = 0; i < a.length; i++) {
      for (var j = 0; j < b.length; j++) {
        cur[j + 1] = a[i] === b[j] ? prev[j] + 1 : Math.max(prev[j + 1], cur[j]);
      }
      var swap = prev; prev = cur; cur = swap;
    }
    return prev[b.length];
  }
  /* Prefill choice: among non-empty candidates (Bengali-script ones when
   * any exist) the reading with most agreement, ties broken by the medoid
   * (smallest summed grapheme edit distance to all other candidates). */
  function consensus(item) {
    var all = okCandidates(item).filter(function (c) { return norm(c.text); });
    if (!all.length) { return null; }
    var pool = all.filter(function (c) { return bengaliRatio(c.text) >= 0.5; });
    if (!pool.length) { pool = all; }
    var counts = agreement(item);
    var split = all.map(function (c) { return graphemes(norm(c.text)); });
    var best = null, bestCount = -1, bestDist = Infinity;
    pool.forEach(function (c) {
      var mine = graphemes(norm(c.text)), dist = 0;
      split.forEach(function (other) {
        dist += mine.length + other.length - 2 * lcsLength(mine, other);
      });
      var count = counts[norm(c.text)];
      if (count > bestCount || (count === bestCount && dist < bestDist)) {
        best = c; bestCount = count; bestDist = dist;
      }
    });
    return best;
  }
  function draftKey(item) { return cropKey(currentPage().page_id, item.region_index); }
  function draftFor(item) {
    var key = draftKey(item);
    if (!state.drafts[key]) {
      var best = consensus(item);
      state.drafts[key] = {
        text: best ? best.text : "", choice: best ? best.label : null,
        reason: "", reasonEdited: false
      };
    }
    return state.drafts[key];
  }
  /* Text actually submitted: whitespace-only means "no text" (noise). */
  function finalText(draft) { return draft.text.trim() ? draft.text : ""; }
  /* The label recorded on submit: only a candidate whose text is exactly
   * the resolved text (the picked one first). */
  function effectiveChoice(item, draft) {
    var text = finalText(draft);
    var picked = draft.choice && byLabel(item, draft.choice);
    if (picked && picked.status === "ok" && picked.text === text) { return picked.label; }
    var ok = okCandidates(item);
    for (var i = 0; i < ok.length; i++) {
      if (ok[i].text === text) { return ok[i].label; }
    }
    return null;
  }
  function autoReason(item, draft) {
    if (!finalText(draft)) { return "Region has no text (noise or stray ink)"; }
    var chosen = effectiveChoice(item, draft);
    if (chosen) { return "Candidate " + chosen + " matches the image"; }
    if (draft.choice) { return "Edited candidate " + draft.choice + " to match the image"; }
    return "Transcribed from the image; no candidate was correct";
  }

  // ---- rendering -----------------------------------------------------
  function text(node, value) { node.textContent = value; return node; }
  function make(tag, className, content) {
    var node = document.createElement(tag);
    if (className) { node.className = className; }
    if (content !== undefined) { node.textContent = content; }
    return node;
  }

  function renderCandidates(item, draft) {
    var counts = agreement(item);
    var chosen = effectiveChoice(item, draft);
    var frag = document.createDocumentFragment();
    item.candidates.forEach(function (c, index) {
      var ok = c.status === "ok";
      var card = make("button", "cand");
      card.type = "button";
      card.disabled = !ok;
      if (!ok) { card.classList.add("is-empty"); }
      if (draft.choice === c.label) { card.classList.add("is-selected"); }
      card.setAttribute("aria-label", "Candidate " + c.label);

      var head = make("span", "cand-head");
      head.appendChild(make("kbd", "", String(index + 1)));
      head.appendChild(make("span", "cand-label", c.label));
      var badges = make("span", "badges");
      if (ok && c.label === chosen) { badges.appendChild(make("span", "badge ok", "✓ resolved")); }
      else if (ok && draft.choice === c.label) { badges.appendChild(make("span", "badge warn", "edited")); }
      var agree = ok ? counts[norm(c.text)] : 0;
      if (agree > 1) { badges.appendChild(make("span", "badge", agree + " agree")); }
      head.appendChild(badges);
      card.appendChild(head);

      var body = make("span", "cand-text");
      body.setAttribute("lang", "bn");
      body.style.display = "block";
      if (!ok) {
        text(body, "no output");
      } else if (!c.text) {
        text(body, "(empty)");
      } else if (!draft.text || c.text === draft.text) {
        text(body, c.text);
      } else {
        diff(draft.text, c.text).forEach(function (op) {
          if (op[0] === "same") { body.appendChild(document.createTextNode(op[1])); }
          else if (op[0] === "ins") { body.appendChild(make("mark", "d-ins", op[1])); }
          else {
            var gap = make("span", "d-del");
            gap.title = "missing here: " + op[1];
            body.appendChild(gap);
          }
        });
      }
      card.appendChild(body);
      card.addEventListener("click", function (event) {
        pick(index);
        event.currentTarget.blur();
      });
      frag.appendChild(card);
    });
    el.candidates.textContent = "";
    el.candidates.appendChild(frag);
  }

  function renderChoiceState(item, draft) {
    var chosen = effectiveChoice(item, draft);
    if (!finalText(draft)) {
      el.choiceState.textContent = "Empty · no text" + (chosen ? " (= " + chosen + ")" : "");
      el.choiceState.className = "chip is-custom";
    } else if (chosen) {
      el.choiceState.textContent = "Candidate " + chosen + (chosen === draft.choice ? "" : " (typed)");
      el.choiceState.className = "chip is-choice";
    } else if (draft.choice) {
      el.choiceState.textContent = "Custom · edited from " + draft.choice;
      el.choiceState.className = "chip is-custom";
    } else {
      el.choiceState.textContent = "Custom text";
      el.choiceState.className = "chip is-custom";
    }
  }

  function syncReason(item, draft) {
    if (!draft.reasonEdited) { draft.reason = autoReason(item, draft); }
    if (el.reason.value !== draft.reason) { el.reason.value = draft.reason; }
  }

  function autosize() {
    el.resolved.style.height = "auto";
    var limit = Math.max(120, window.innerHeight * 0.4);
    el.resolved.style.height = Math.min(el.resolved.scrollHeight + 2, limit) + "px";
  }

  function updateSubmit() {
    var item = currentItem();
    var draft = item ? draftFor(item) : null;
    el.submit.disabled = state.busy || !draft || !el.reason.value.trim();
    el.prev.disabled = state.busy;
    el.skip.disabled = state.busy;
  }

  function refreshItemView() {
    var item = currentItem();
    if (!item) { return; }
    var draft = draftFor(item);
    renderCandidates(item, draft);
    renderChoiceState(item, draft);
    syncReason(item, draft);
    updateSubmit();
  }

  function renderItem() {
    var page = currentPage(), item = currentItem();
    if (!page || !item) { return; }
    var draft = draftFor(item);
    el.regionLabel.textContent = "Region #" + item.region_index;
    el.pageLabel.textContent = "page " + page.page_id.slice(0, 10) + "…";
    el.pageLabel.title = page.page_id;
    el.resolved.value = draft.text;
    autosize();
    refreshItemView();
    showCrop(page.page_id, item.region_index);
    var next = state.items[state.itemIdx + 1];
    if (next) { cropUrl(page.page_id, next.region_index).catch(function () {}); }
    renderProgress();
  }

  function renderProgress() {
    var s = state.summary;
    el.pPage.textContent = state.pageIdx >= 0 ? String(state.pageIdx + 1) : "–";
    el.pPages.textContent = String(state.pages.length || "–");
    el.pItem.textContent = state.items.length ? String(state.itemIdx + 1) : "–";
    el.pItems.textContent = state.items.length ? String(state.items.length) : "–";
    if (s) {
      var total = s.resolved + s.open_conflicts;
      el.pDone.textContent = String(s.resolved);
      el.pTotal.textContent = String(total);
      el.pBar.style.width = (total ? (100 * s.resolved / total) : 100).toFixed(1) + "%";
    }
    renderPageSelect();
  }

  function renderPageSelect() {
    var select = el.pageSelect;
    if (select.options.length !== state.pages.length) {
      select.textContent = "";
      state.pages.forEach(function (_, i) { select.appendChild(make("option")); });
    }
    state.pages.forEach(function (page, i) {
      var option = select.options[i];
      var open = page.open_conflicts;
      option.value = String(i);
      option.textContent = "Page " + (i + 1) + " · " + page.page_id.slice(0, 8)
        + (open ? " — " + open + " open" : " — done ✓");
      option.disabled = !open && i !== state.pageIdx;
    });
    if (state.pageIdx >= 0) { select.value = String(state.pageIdx); }
  }

  // ---- views -----------------------------------------------------------
  function showView(name) {
    el.login.hidden = name !== "login";
    el.workspace.hidden = name !== "work";
    el.actionbar.hidden = name !== "work";
    el.done.hidden = name !== "done";
  }
  function showLogin(message) {
    showView("login");
    el.loginError.textContent = message || "";
    el.token.value = "";
    el.token.focus();
  }
  function showDone() {
    state.items = [];
    showView("done");
    var s = state.summary;
    el.doneDetail.textContent = s
      ? s.resolved + " conflicts resolved across " + s.pages + " pages."
      : "Nothing left to adjudicate.";
    renderProgress();
  }

  var toastTimer = null;
  function toast(message, isError) {
    el.toast.textContent = message;
    el.toast.className = "toast show" + (isError ? " err" : "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.toast.className = "toast" + (isError ? " err" : ""); },
                            isError ? 4200 : 2200);
  }

  function handleError(error) {
    if (error && error.status === 403) {
      showLogin("Token rejected. Paste the current adjudicator token from the server log.");
    } else {
      toast(String((error && error.message) || error), true);
    }
  }

  // ---- navigation ------------------------------------------------------
  async function refreshPages() {
    var data = await api("GET", "/api/study/pages");
    state.pages = data.pages || [];
    state.summary = data.summary || null;
  }

  function nextOpenPage(from, direction) {
    var n = state.pages.length;
    for (var step = 1; step <= n; step++) {
      var i = (((from + direction * step) % n) + n) % n;
      if (state.pages[i].open_conflicts > 0) { return i; }
    }
    return -1;
  }

  async function openPage(index, focus) {
    var page = state.pages[index];
    var data = await api("GET", "/api/study/conflicts/" + encodeURIComponent(page.page_id));
    state.pageIdx = index;
    state.items = data.items || [];
    page.open_conflicts = state.items.length;
    if (!state.items.length) {
      // Move on using local counts only (no refresh), so a page whose
      // server count disagrees with its list cannot loop forever.
      var next = nextOpenPage(index, 1);
      return next < 0 ? showDone() : openPage(next, focus);
    }
    state.itemIdx = 0;
    if (focus === "last") { state.itemIdx = state.items.length - 1; }
    else if (typeof focus === "number") {
      state.items.forEach(function (item, i) { if (item.region_index === focus) { state.itemIdx = i; } });
    }
    showView("work");
    renderItem();
  }

  async function advancePage(direction, focus) {
    await refreshPages();
    var index = nextOpenPage(state.pageIdx < 0 ? -1 : state.pageIdx, direction);
    if (index < 0) { return showDone(); }
    return openPage(index, focus);
  }

  /* Serialise navigation/submission so key-mashing cannot double-post. */
  async function guarded(task) {
    if (state.busy) { return; }
    state.busy = true;
    updateSubmit();
    try { await task(); }
    catch (error) { handleError(error); }
    finally { state.busy = false; updateSubmit(); }
  }

  function nextItem() {
    return guarded(async function () {
      if (state.itemIdx + 1 < state.items.length) { state.itemIdx++; renderItem(); }
      else { await advancePage(1); }
    });
  }
  function prevItem() {
    return guarded(async function () {
      if (state.itemIdx > 0) { state.itemIdx--; renderItem(); }
      else { await advancePage(-1, "last"); }
    });
  }
  function jumpPage(direction) {
    return guarded(function () { return advancePage(direction); });
  }

  function pick(index) {
    var item = currentItem();
    if (!item || !item.candidates[index]) { return; }
    var c = item.candidates[index];
    if (c.status !== "ok") { toast("Candidate " + c.label + " has no output", true); return; }
    var draft = draftFor(item);
    draft.text = c.text;
    draft.choice = c.label;
    el.resolved.value = c.text;
    autosize();
    refreshItemView();
  }

  /* "No text": the region holds only noise / stray ink. */
  function markEmpty() {
    var item = currentItem();
    if (!item) { return; }
    var draft = draftFor(item);
    draft.text = "";
    draft.choice = null;
    el.resolved.value = "";
    autosize();
    refreshItemView();
  }

  function submit() {
    var item = currentItem(), page = currentPage();
    if (!item || !page || state.busy) { return; }
    var draft = draftFor(item);
    var reason = el.reason.value.trim();
    if (!reason) { toast("A reason is required", true); el.reason.focus(); return; }
    var chosen = effectiveChoice(item, draft);
    var body = {
      region_index: item.region_index, resolved_text: finalText(draft),
      reason: reason, revision: item.revision
    };
    if (chosen) { body.choice_label = chosen; }
    el.submit.textContent = "Saving…";
    return guarded(async function () {
      try {
        await api("POST", "/api/study/adjudicate/" + encodeURIComponent(page.page_id), body);
      } catch (error) {
        if (error.status === 409) {
          delete state.drafts[draftKey(item)];
          toast("This region changed elsewhere; reloaded it", true);
          await openPage(state.pageIdx, item.region_index);
          return;
        }
        throw error;
      }
      delete state.drafts[draftKey(item)];
      state.items.splice(state.itemIdx, 1);
      page.open_conflicts = state.items.length;
      if (state.summary) {
        state.summary.resolved += 1;
        state.summary.open_conflicts = Math.max(0, state.summary.open_conflicts - 1);
      }
      toast("Saved region #" + item.region_index + (!body.resolved_text ? " · no text"
            : chosen ? " · " + chosen : " · custom text"));
      el.resolved.blur();
      el.reason.blur();
      if (state.itemIdx < state.items.length) { renderItem(); }
      else { await advancePage(1); }
    }).finally(function () {
      el.submit.innerHTML = "Submit <kbd>Ctrl ↵</kbd>";
      updateSubmit();
    });
  }

  // ---- text size / overlays -----------------------------------------------
  function sizeIndex() {
    var stored = parseInt(load(SIZE_KEY), 10);
    return isNaN(stored) ? 2 : Math.max(0, Math.min(TEXT_SIZES.length - 1, stored));
  }
  function applySize(index) {
    save(SIZE_KEY, String(index));
    document.documentElement.style.setProperty("--bn-size", TEXT_SIZES[index] + "rem");
    autosize();
  }
  /* Modal overlays take focus on open and hand it back on close. */
  var overlayReturn = null;
  function openOverlay(node) {
    overlayReturn = document.activeElement;
    node.hidden = false;
    (node.querySelector(".sheet") || node).focus();
  }
  function closeOverlay(node) {
    node.hidden = true;
    if (overlayReturn && overlayReturn !== document.body && overlayReturn.focus) {
      overlayReturn.focus();
    }
    overlayReturn = null;
  }
  function showZoom() {
    if (el.zoomImg.getAttribute("src")) { openOverlay(el.zoom); el.zoom.scrollTop = 0; }
  }
  function focusEnd(field) {
    field.focus();
    var end = field.value.length;
    try { field.setSelectionRange(end, end); } catch (e) { /* not a text field */ }
  }

  // ---- events --------------------------------------------------------------
  el.resolved.addEventListener("input", function () {
    var item = currentItem();
    if (!item) { return; }
    draftFor(item).text = el.resolved.value;
    autosize();
    clearTimeout(el.resolved._timer);
    el.resolved._timer = setTimeout(refreshItemView, 120);
    updateSubmit();
  });
  el.reason.addEventListener("input", function () {
    var item = currentItem();
    if (!item) { return; }
    var draft = draftFor(item);
    draft.reason = el.reason.value;
    draft.reasonEdited = true;
    updateSubmit();
  });
  el.submit.addEventListener("click", function (event) { event.currentTarget.blur(); submit(); });
  el.skip.addEventListener("click", function (event) { event.currentTarget.blur(); nextItem(); });
  el.prev.addEventListener("click", function (event) { event.currentTarget.blur(); prevItem(); });
  el.emptyBtn.addEventListener("click", function (event) { event.currentTarget.blur(); markEmpty(); });
  el.cropFrame.addEventListener("click", showZoom);
  el.zoom.addEventListener("click", function () { closeOverlay(el.zoom); });
  el.help.addEventListener("click", function (event) { if (event.target === el.help) { closeOverlay(el.help); } });
  el.helpBtn.addEventListener("click", function (event) { event.currentTarget.blur(); openOverlay(el.help); });
  el.tokenBtn.addEventListener("click", function () { showLogin(""); });
  el.pageSelect.addEventListener("change", function () {
    var index = parseInt(el.pageSelect.value, 10);
    el.pageSelect.blur();
    guarded(function () { return openPage(index); });
  });
  el.loginForm.addEventListener("submit", function (event) {
    event.preventDefault();
    var value = el.token.value.trim();
    if (!value) { el.loginError.textContent = "Paste a token first."; return; }
    save(TOKEN_KEY, value);
    el.token.value = "";
    start();
  });
  window.addEventListener("resize", autosize);

  document.addEventListener("keydown", function (event) {
    // Never act on keys that confirm an IME composition (Bengali input).
    if (event.isComposing || event.keyCode === 229) { return; }
    var key = event.key;
    if (!el.help.hidden || !el.zoom.hidden) {
      // Keep keyboard focus inside the open modal overlay.
      if (key === "Tab") { event.preventDefault(); return; }
    }
    if (!el.help.hidden) {
      if (key === "Escape" || key === "?") { closeOverlay(el.help); event.preventDefault(); }
      return;
    }
    if (!el.zoom.hidden) {
      if (key === "Escape" || key === "z" || key === "Z") { closeOverlay(el.zoom); event.preventDefault(); }
      return;
    }
    if (el.workspace.hidden) { return; }
    var target = event.target;
    var tag = target && target.tagName;
    var inField = tag === "TEXTAREA" || tag === "INPUT" || tag === "SELECT";
    if ((event.ctrlKey || event.metaKey) && key === "Enter") {
      event.preventDefault();
      submit();
      return;
    }
    if (event.altKey && !event.ctrlKey && !event.metaKey && /^Digit[0-5]$/.test(event.code)) {
      event.preventDefault();
      if (event.code === "Digit0") { markEmpty(); } else { pick(Number(event.code.slice(5)) - 1); }
      return;
    }
    if (inField) {
      if (key === "Escape") { target.blur(); event.preventDefault(); }
      else if (key === "Enter" && target === el.reason) { event.preventDefault(); submit(); }
      return;
    }
    if (event.ctrlKey || event.metaKey || event.altKey) { return; }
    if (tag === "BUTTON" && (key === "Enter" || key === " ")) { return; }
    var handled = true;
    if (key >= "1" && key <= "5") { pick(Number(key) - 1); }
    else if (key === "0" || key === "n" || key === "N") { markEmpty(); }
    else if (/^[a-eA-E]$/.test(key)) { pick(key.toUpperCase().charCodeAt(0) - 65); }
    else if (key === "Enter") { submit(); }
    else if (key === "t" || key === "T") { focusEnd(el.resolved); }
    else if (key === "r" || key === "R") { focusEnd(el.reason); }
    else if (key === "s" || key === "S" || key === "j" || key === "J" || key === "ArrowRight") { nextItem(); }
    else if (key === "k" || key === "K" || key === "ArrowLeft") { prevItem(); }
    else if (key === "]") { jumpPage(1); }
    else if (key === "[") { jumpPage(-1); }
    else if (key === "z" || key === "Z") { showZoom(); }
    else if (key === "+" || key === "=") { applySize(Math.min(TEXT_SIZES.length - 1, sizeIndex() + 1)); }
    else if (key === "-" || key === "_") { applySize(Math.max(0, sizeIndex() - 1)); }
    else if (key === "?") { openOverlay(el.help); }
    else { handled = false; }
    if (handled) { event.preventDefault(); }
  });

  // ---- boot ------------------------------------------------------------------
  async function start() {
    if (!load(TOKEN_KEY)) { showLogin(""); return; }
    state.pageIdx = -1;
    state.drafts = {};
    await guarded(function () { return advancePage(1); });
  }

  var fromHash = /(?:^#|&)token=([^&]+)/.exec(window.location.hash || "");
  if (fromHash) {
    save(TOKEN_KEY, decodeURIComponent(fromHash[1]));
    try { history.replaceState(null, "", window.location.pathname + window.location.search); }
    catch (e) { window.location.hash = ""; }
  }
  applySize(sizeIndex());
  start();
})();
