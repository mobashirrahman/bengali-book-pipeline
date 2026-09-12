/* Blind annotation client: crops for transcribers, full page for review. */
"use strict";

const TOKEN_KEY = "annot.token";

const state = {
  token: "",
  role: "",
  userId: "",
  pageRef: "",
  pageId: "",
  alias: "",
  revision: 0,
  slots: [],
  order: [],
  imageSize: null,
  texts: {},
  ownProposals: [],
  activePos: 0,
  reportMode: false,
  dragStart: null,
  dragLayer: null,
  pendingGeom: null,
  coverageQueue: [],
  coverageSignoff: null,
  lastTick: 0,
  lastTap: 0,
};

function headers() {
  return {
    "Content-Type": "application/json",
    "X-Annotation-Token": state.token,
  };
}

function statusLine(text) {
  document.getElementById("status").textContent = text;
}

function elapsedSinceTick() {
  const now = performance.now();
  const delta = Math.max(0, Math.round(now - state.lastTick));
  state.lastTick = now;
  return delta;
}

function isNarrow() {
  return window.matchMedia("(max-width: 60rem)").matches;
}

function showLogin(message) {
  state.token = "";
  try {
    window.localStorage.removeItem(TOKEN_KEY);
  } catch (err) {
    /* private mode: stay logged in for this tab only */
  }
  document.getElementById("login-form").hidden = false;
  document.getElementById("picker-wrap").hidden = true;
  document.getElementById("work").hidden = true;
  document.getElementById("image-area").hidden = true;
  document.getElementById("review").hidden = true;
  document.getElementById("coverage").hidden = true;
  if (message) statusLine(message);
}

async function login() {
  const userId = document.getElementById("user-id").value.trim();
  const pin = document.getElementById("pin").value;
  if (!userId || !pin) {
    statusLine("Type your name and PIN first.");
    return;
  }
  const response = await fetch("/api/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user_id: userId, pin: pin }),
  });
  const body = await response.json().catch(() => ({}));
  if (response.status === 429) {
    statusLine("Too many wrong PINs — wait a few minutes and retry.");
    return;
  }
  if (!response.ok) {
    statusLine(`Login failed: ${body.error || response.status}`);
    return;
  }
  state.token = body.token;
  try {
    window.localStorage.setItem(TOKEN_KEY, body.token);
  } catch (err) {
    /* private mode: token lives for this tab only */
  }
  document.getElementById("pin").value = "";
  await enterApp();
}

async function enterApp() {
  const response = await fetch("/api/session", { headers: headers() });
  if (!response.ok) {
    showLogin("Session expired — log in again.");
    return;
  }
  const session = await response.json();
  if (!session.role) {
    showLogin("Session expired — log in again.");
    return;
  }
  state.role = session.role;
  state.userId = session.user_id || "";
  document.getElementById("login-form").hidden = true;
  document.getElementById("picker-wrap").hidden = false;
  document.getElementById("page-picker").hidden = state.role !== "annotator";
  document.getElementById("load").hidden = state.role !== "annotator";
  document.getElementById("whoami").textContent =
    `Logged in as ${state.userId} (${state.role}).`;
  const isReviewer = state.role === "adjudicator" ||
    state.role === "coverage";
  document.getElementById("review").hidden = !isReviewer;
  document.getElementById("coverage").hidden = state.role !== "coverage";
  await refreshSession();
  if (state.role === "annotator") {
    await loadMyPages();
    statusLine("Pick one of your pages and load it.");
  } else if (state.role === "coverage") {
    statusLine("Load the coverage queue below, then open pages to check.");
  } else {
    statusLine("Reviewer mode: open a page from the proposal queue below.");
  }
}

function statusWord(entry) {
  if (entry.status === "submitted" || entry.status === "adjudicated") {
    return "submitted";
  }
  if (entry.status === "draft" || entry.drafted_regions > 0) {
    return `draft ${entry.drafted_regions}/${entry.total_regions}`;
  }
  return "to do";
}

async function loadMyPages(selectAlias) {
  const picker = document.getElementById("page-picker");
  picker.innerHTML = "";
  const response = await fetch("/api/my-pages", { headers: headers() });
  if (!response.ok) {
    if (response.status === 403) showLogin("Session expired — log in again.");
    return;
  }
  const body = await response.json();
  for (const entry of body.pages || []) {
    const option = document.createElement("option");
    option.value = entry.alias;
    option.textContent = `${entry.alias} — ${statusWord(entry)}`;
    picker.appendChild(option);
  }
  if (selectAlias) picker.value = selectAlias;
}

async function refreshSession() {
  const response = await fetch("/api/session", { headers: headers() });
  if (!response.ok) return;
  const session = await response.json();
  state.role = session.role || "";
  state.userId = session.user_id || state.userId;
  const progress = session.progress || {};
  const line = `pages: ${progress.pages ?? "?"} | ` +
    `submitted: ${progress.submitted_assignments ?? "?"} | ` +
    `open conflicts: ${progress.open_conflicts ?? "?"}`;
  document.getElementById("progress").textContent = line;
  const isReviewer = state.role === "adjudicator" ||
    state.role === "coverage";
  document.getElementById("review").hidden = !isReviewer;
  document.getElementById("coverage").hidden = state.role !== "coverage";
}

function slotByIndex(index) {
  return state.slots.find((slot) => slot.region_index === index) || null;
}

async function loadPageByRef(ref) {
  const clean = String(ref || "").trim();
  if (!clean) {
    statusLine("Pick one of your pages first.");
    return;
  }
  if (!state.token) {
    showLogin("Log in first.");
    return;
  }
  state.pageRef = clean;
  statusLine("Loading blind payload…");
  const response = await fetch(
    `/api/page/${encodeURIComponent(clean)}`,
    { headers: headers() },
  );
  if (response.status === 403) {
    showLogin("Session expired — log in again.");
    return;
  }
  if (response.status === 404) {
    statusLine(`Unknown page ${clean}.`);
    return;
  }
  if (!response.ok) {
    statusLine(`Load failed (${response.status}).`);
    return;
  }
  const payload = await response.json();
  state.pageId = payload.page_id;
  state.alias = payload.alias || clean;
  state.revision = payload.revision;
  state.slots = payload.line_slots || [];
  state.order = (payload.reading_order && payload.reading_order.length)
    ? payload.reading_order
    : state.slots.map((slot) => slot.region_index);
  state.imageSize = payload.image_size || null;
  state.texts = Object.assign({}, payload.drafts || {});
  state.ownProposals = payload.own_proposals || [];
  state.activePos = 0;
  for (let pos = 0; pos < state.order.length; pos += 1) {
    if (!(String(state.order[pos]) in state.texts)) {
      state.activePos = pos;
      break;
    }
  }
  state.reportMode = false;
  state.pendingGeom = null;
  document.querySelector("main").classList.add("wide");
  const shortHash = (payload.image_sha256 || "").slice(0, 8);
  document.getElementById("image-hash").textContent =
    `${state.alias} · frozen scan ${shortHash}…`;
  document.getElementById("image-hash").title =
    `full sha256: ${payload.image_sha256 || ""}`;
  document.getElementById("proposal-form").hidden = true;
  if (state.role === "annotator") {
    document.getElementById("image-area").hidden = true;
    document.getElementById("work").hidden = state.slots.length === 0;
    renderStepper();
    await loadCrop();
    statusLine(
      payload.assignment_status === "submitted"
        ? "This page is already submitted. Edits will reopen it as a draft."
        : `Loaded ${state.alias} (${state.slots.length} regions). One at a time.`,
    );
  } else {
    document.getElementById("work").hidden = true;
    document.getElementById("image-area").hidden = false;
    await loadImage();
    renderOverlay();
    document.getElementById("report-toggle").disabled = false;
    document.getElementById("zoom-toggle").disabled = false;
    statusLine(`Loaded ${state.alias} for review.`);
  }
  state.lastTick = performance.now();
  await refreshSession();
}

async function blobUrl(url) {
  const response = await fetch(url, { headers: headers() });
  if (!response.ok) return null;
  const blob = await response.blob();
  return URL.createObjectURL(blob);
}

async function loadCrop() {
  const index = activeRegionIndex();
  const image = document.getElementById("crop-image");
  const missing = document.getElementById("crop-missing");
  const wrap = document.getElementById("crop-wrap");
  wrap.hidden = false;
  missing.hidden = true;
  image.alt = `Region ${index} crop — loading…`;
  const url = `/api/page/${encodeURIComponent(state.pageRef)}` +
    `/region/${encodeURIComponent(index)}/image` +
    `?token=${encodeURIComponent(state.token)}`;
  const objectUrl = await blobUrl(url);
  if (objectUrl === null) {
    missing.hidden = false;
    return;
  }
  image.src = objectUrl;
  image.hidden = false;
  image.alt = `Region ${index} crop`;
  renderCropOverlay();
}

function renderCropOverlay() {
  const layer = document.getElementById("crop-overlay");
  layer.innerHTML = "";
  const drag = document.createElement("div");
  drag.id = "drag-rect";
  drag.hidden = true;
  layer.appendChild(drag);
}

async function loadImage() {
  const image = document.getElementById("page-image");
  const missing = document.getElementById("image-missing");
  const imageUrl = `/api/page/${encodeURIComponent(state.pageRef)}/image` +
    `?token=${encodeURIComponent(state.token)}`;
  try {
    const probe = await fetch(imageUrl, { headers: headers() });
    if (probe.ok) {
      const blob = await probe.blob();
      image.src = URL.createObjectURL(blob);
      image.hidden = false;
      missing.hidden = true;
    } else {
      image.hidden = true;
      missing.hidden = false;
    }
  } catch (err) {
    image.hidden = true;
    missing.hidden = false;
  }
}

function boxPercent(geometry) {
  const size = state.imageSize;
  if (!size) return null;
  return {
    left: (geometry.x0 / size.width) * 100,
    top: (geometry.y0 / size.height) * 100,
    width: ((geometry.x1 - geometry.x0) / size.width) * 100,
    height: ((geometry.y1 - geometry.y0) / size.height) * 100,
  };
}

function renderOverlay() {
  const layer = document.getElementById("overlay");
  layer.innerHTML = "";
  const activeIndex = state.order[state.activePos];
  for (const slot of state.slots) {
    const box = boxPercent(slot.geometry);
    if (!box) continue;
    const div = document.createElement("div");
    div.className = "region-box" +
      (slot.region_index === activeIndex ? " active" : "");
    div.style.left = `${box.left}%`;
    div.style.top = `${box.top}%`;
    div.style.width = `${box.width}%`;
    div.style.height = `${box.height}%`;
    div.title = `Region ${slot.region_index} — tap to jump`;
    div.dataset.region = String(slot.region_index);
    div.addEventListener("click", (event) => {
      if (state.reportMode) return;
      event.stopPropagation();
      jumpToRegion(slot.region_index);
    });
    layer.appendChild(div);
  }
  for (const proposal of state.ownProposals) {
    const box = boxPercent(proposal.geometry);
    if (!box) continue;
    const div = document.createElement("div");
    div.className = "region-box proposed";
    div.style.left = `${box.left}%`;
    div.style.top = `${box.top}%`;
    div.style.width = `${box.width}%`;
    div.style.height = `${box.height}%`;
    div.title = "Your pending proposal — awaiting reviewer";
    layer.appendChild(div);
  }
  const drag = document.createElement("div");
  drag.id = "drag-rect";
  drag.hidden = true;
  layer.appendChild(drag);
}

function activeRegionIndex() {
  return state.order[state.activePos];
}

function alignRegionToPanel() {
  if (document.getElementById("image-area").hidden) return;
  const index = activeRegionIndex();
  if (index === undefined) return;
  const box = document.querySelector(
    `#overlay .region-box[data-region="${index}"]`);
  if (!box) return;
  const boxRect = box.getBoundingClientRect();
  if (boxRect.width === 0 && boxRect.height === 0) return;
  if (isNarrow()) {
    const zoom = document.getElementById("zoom-scroll");
    const view = zoom.getBoundingClientRect();
    zoom.scrollTop +=
      (boxRect.top + boxRect.height / 2) - (view.top + view.height / 2);
    zoom.scrollLeft +=
      (boxRect.left + boxRect.width / 2) - (view.left + view.width / 2);
    return;
  }
  const panel = document.getElementById("work");
  if (!panel || panel.hidden) {
    box.scrollIntoView({ block: "center", behavior: "smooth" });
    return;
  }
  const panelRect = panel.getBoundingClientRect();
  const delta = (boxRect.top + boxRect.height / 2) -
    (panelRect.top + Math.min(panelRect.height, window.innerHeight) / 2);
  if (Math.abs(delta) > 4) {
    window.scrollBy({ top: delta, behavior: "smooth" });
  }
}

function renderStepper() {
  const total = state.order.length;
  const label = document.getElementById("region-label");
  const area = document.getElementById("region-text");
  if (total === 0) {
    label.textContent = "No regions.";
    area.value = "";
    area.disabled = true;
    return;
  }
  const index = activeRegionIndex();
  label.textContent =
    `${state.alias} · Region ${index} (${state.activePos + 1} of ${total})`;
  area.disabled = false;
  area.value = state.texts[String(index)] || "";
  area.setAttribute("aria-label", `Transcription for region ${index}`);
  document.getElementById("prev-region").disabled = state.activePos === 0;
  document.getElementById("next-region").disabled =
    state.activePos >= total - 1;
  const done = Object.keys(state.texts).length;
  document.getElementById("draft-count").textContent =
    `${done} of ${total} regions have text.`;
}

function stashActiveText() {
  if (state.order.length === 0) return;
  const area = document.getElementById("region-text");
  state.texts[String(activeRegionIndex())] = area.value;
}

function jumpToRegion(index) {
  stashActiveText();
  const pos = state.order.indexOf(index);
  if (pos >= 0) {
    state.activePos = pos;
    showCrop();
    renderStepper();
  }
}

function showCrop() {
  document.getElementById("image-area").hidden = true;
  document.getElementById("crop-wrap").hidden = false;
  loadCrop();
}

async function authedFetch(url, options) {
  const response = await fetch(url, options);
  if (response.status === 403) {
    showLogin("Session expired — log in again.");
    return null;
  }
  return response;
}

async function saveDraft(silent) {
  stashActiveText();
  const response = await authedFetch(
    `/api/page/${encodeURIComponent(state.pageRef)}/submit`,
    {
      method: "POST",
      headers: headers(),
      body: JSON.stringify({
        revision: state.revision,
        lines: state.texts,
        elapsed_ms: elapsedSinceTick(),
      }),
    },
  );
  if (response === null) return null;
  const body = await response.json().catch(() => ({}));
  if (response.status === 409) {
    statusLine(
      "The page changed under you (for example, a reviewer approved a " +
      "new region). Reloading — your typed text above is kept in the boxes.",
    );
    await loadPageByRef(state.pageRef);
    return null;
  }
  if (!response.ok) {
    statusLine(`Save failed (${response.status}): ${body.error || ""}`);
    return null;
  }
  state.revision = body.assignment.revision;
  state.lastTick = performance.now();
  if (!silent) {
    const total = state.order.length;
    const done = Object.keys(state.texts).length;
    statusLine(
      body.assignment.status === "submitted"
        ? "Page submitted. Thank you — load the next page."
        : `Draft saved (${done} of ${total} regions). You can stop anytime.`,
    );
  }
  await refreshSession();
  if (state.role === "annotator") await loadMyPages(state.alias);
  return body.assignment.status;
}

async function saveAndNext() {
  const result = await saveDraft(true);
  if (result === null) return;
  if (result === "submitted") {
    statusLine("Page submitted. Thank you — load the next page.");
    renderStepper();
    return;
  }
  if (state.activePos < state.order.length - 1) {
    state.activePos += 1;
  }
  const total = state.order.length;
  const done = Object.keys(state.texts).length;
  statusLine(`Draft saved (${done} of ${total} regions). Region ${state.activePos + 1}.`);
  showCrop();
  renderStepper();
}

async function submitPage() {
  stashActiveText();
  const total = state.order.length;
  const missing = state.order.filter(
    (index) => !(String(index) in state.texts),
  );
  if (missing.length > 0) {
    statusLine(
      `Cannot submit yet: regions ${missing.join(", ")} still have no text.`,
    );
    return;
  }
  const result = await saveDraft(true);
  if (result === "submitted") {
    statusLine("Page submitted. Thank you — load the next page.");
  } else if (result !== null) {
    statusLine("Saved as draft — the server wants more regions first.");
  }
  renderStepper();
}

function setReportMode(on) {
  state.reportMode = on;
  state.pendingGeom = null;
  document.getElementById("proposal-form").hidden = true;
  const button = document.getElementById("report-toggle");
  button.textContent = on
    ? "Exit missed-text mode"
    : "Report missed text";
  document.getElementById("report-hint").hidden = !on;
  document.getElementById("overlay").classList.toggle("reporting", on);
  document.getElementById("crop-overlay").classList.toggle("reporting", on);
}

function setZoom(on) {
  const zoom = document.getElementById("zoom-scroll");
  zoom.classList.toggle("zoomed", on);
  document.getElementById("zoom-toggle").textContent = on
    ? "Zoom out"
    : "Zoom in";
  if (on) alignRegionToPanel();
}

function layerFraction(layer, event) {
  const rect = layer.getBoundingClientRect();
  return {
    x: Math.min(Math.max((event.clientX - rect.left) / rect.width, 0), 1),
    y: Math.min(Math.max((event.clientY - rect.top) / rect.height, 0), 1),
  };
}

function minBoxPx() {
  return window.matchMedia("(pointer: coarse)").matches ? 12 : 5;
}

function cropToPageGeometry(fraction) {
  const slot = slotByIndex(activeRegionIndex());
  const crop = slot && slot.crop_box;
  if (!crop) return null;
  return {
    x0: Math.max(0, Math.floor(crop.x0 + fraction.x0 * (crop.x1 - crop.x0))),
    y0: Math.max(0, Math.floor(crop.y0 + fraction.y0 * (crop.y1 - crop.y0))),
    x1: Math.ceil(crop.x0 + fraction.x1 * (crop.x1 - crop.x0)),
    y1: Math.ceil(crop.y0 + fraction.y1 * (crop.y1 - crop.y0)),
    unit: "px",
  };
}

function bindDrag(layerId, toPageGeometry) {
  const layer = document.getElementById(layerId);
  layer.addEventListener("pointerdown", (event) => {
    if (!state.reportMode || !state.imageSize) return;
    event.preventDefault();
    try {
      layer.setPointerCapture(event.pointerId);
    } catch (err) {
      /* older browsers: fall through to mouse-compatible behaviour */
    }
    state.dragStart = layerFraction(layer, event);
    state.dragLayer = layerId;
    const drag = layer.querySelector("#drag-rect");
    if (drag) drag.hidden = false;
  });
  layer.addEventListener("pointermove", (event) => {
    if (!state.reportMode || !state.dragStart) return;
    if (state.dragLayer !== layerId) return;
    if (event.buttons !== undefined && event.buttons === 0 &&
        event.pointerType === "mouse") {
      return;
    }
    const current = layerFraction(layer, event);
    const drag = layer.querySelector("#drag-rect");
    if (!drag) return;
    drag.style.left = `${Math.min(state.dragStart.x, current.x) * 100}%`;
    drag.style.top = `${Math.min(state.dragStart.y, current.y) * 100}%`;
    drag.style.width =
      `${Math.abs(current.x - state.dragStart.x) * 100}%`;
    drag.style.height =
      `${Math.abs(current.y - state.dragStart.y) * 100}%`;
  });
  const finishDrag = (event) => {
    if (!state.reportMode || !state.dragStart) return;
    if (state.dragLayer !== layerId) return;
    const start = state.dragStart;
    state.dragStart = null;
    state.dragLayer = null;
    const drag = layer.querySelector("#drag-rect");
    if (drag) drag.hidden = true;
    const end = layerFraction(layer, event);
    const fraction = {
      x0: Math.min(start.x, end.x),
      y0: Math.min(start.y, end.y),
      x1: Math.max(start.x, end.x),
      y1: Math.max(start.y, end.y),
    };
    const geometry = toPageGeometry(fraction);
    if (!geometry) {
      statusLine("Crop geometry unavailable — use the full-page view.");
      return;
    }
    // Clamp into the frozen image and enforce a minimum size.
    const size = state.imageSize;
    geometry.x0 = Math.max(0, geometry.x0);
    geometry.y0 = Math.max(0, geometry.y0);
    geometry.x1 = Math.min(size.width, geometry.x1);
    geometry.y1 = Math.min(size.height, geometry.y1);
    if (geometry.x1 - geometry.x0 < minBoxPx() ||
        geometry.y1 - geometry.y0 < minBoxPx()) {
      statusLine("Box too small — draw a rectangle around the missed text.");
      return;
    }
    state.pendingGeom = geometry;
    const form = document.getElementById("proposal-form");
    form.hidden = false;
    document.getElementById("proposal-note").value = "";
    document.getElementById("proposal-note").focus();
    statusLine("Box drawn — add an optional note and send, or cancel.");
  };
  layer.addEventListener("pointerup", finishDrag);
  layer.addEventListener("pointercancel", () => {
    if (state.dragLayer !== layerId) return;
    state.dragStart = null;
    state.dragLayer = null;
    const drag = layer.querySelector("#drag-rect");
    if (drag) drag.hidden = true;
  });
}

function fullToPageGeometry(fraction) {
  const size = state.imageSize;
  if (!size) return null;
  return {
    x0: Math.max(0, Math.floor(fraction.x0 * size.width)),
    y0: Math.max(0, Math.floor(fraction.y0 * size.height)),
    x1: Math.min(size.width, Math.ceil(fraction.x1 * size.width)),
    y1: Math.min(size.height, Math.ceil(fraction.y1 * size.height)),
    unit: "px",
  };
}

async function sendProposal() {
  if (!state.pendingGeom) {
    statusLine("Draw a rectangle on the scan first.");
    return;
  }
  const note = document.getElementById("proposal-note").value;
  const response = await authedFetch(
    `/api/page/${encodeURIComponent(state.pageRef)}/propose-region`,
    {
      method: "POST",
      headers: headers(),
      body: JSON.stringify({ geometry: state.pendingGeom, note: note }),
    },
  );
  if (response === null) return;
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    statusLine(`Proposal failed (${response.status}): ${body.error || ""}`);
    return;
  }
  state.ownProposals.push({
    id: body.proposal.id,
    geometry: body.proposal.geometry,
    note: body.proposal.note,
  });
  state.pendingGeom = null;
  document.getElementById("proposal-form").hidden = true;
  if (!document.getElementById("image-area").hidden) renderOverlay();
  statusLine(
    "Proposal sent. A reviewer decides; it becomes a transcription " +
    "box only after approval.",
  );
}

function bindZoomTap() {
  const zoom = document.getElementById("zoom-scroll");
  zoom.addEventListener("click", (event) => {
    if (state.reportMode) return;
    if (event.target.closest(".region-box")) return;
    const now = performance.now();
    if (now - state.lastTap < 350) {
      state.lastTap = 0;
      setZoom(!zoom.classList.contains("zoomed"));
    } else {
      state.lastTap = now;
    }
  });
}

async function loadProposals() {
  const list = document.getElementById("proposal-list");
  list.innerHTML = "";
  const response = await authedFetch("/api/proposals?status=pending", {
    headers: headers(),
  });
  if (response === null) return;
  if (!response.ok) {
    statusLine(`Proposal queue failed (${response.status}).`);
    return;
  }
  const body = await response.json();
  const proposals = body.proposals || [];
  if (proposals.length === 0) {
    list.textContent = "No pending proposals.";
    return;
  }
  for (const proposal of proposals) {
    const item = document.createElement("div");
    item.className = "proposal-item";
    const title = document.createElement("div");
    title.className = "mono";
    title.textContent = `#${proposal.id} page ${proposal.page_id} ` +
      `from ${proposal.reporter_id}`;
    const note = document.createElement("div");
    note.textContent = proposal.note || "(no note)";
    const load = document.createElement("button");
    load.type = "button";
    load.textContent = "Open page";
    load.addEventListener("click", () => {
      loadPageByRef(proposal.page_id);
    });
    const reason = document.createElement("input");
    reason.type = "text";
    reason.placeholder = "reason (required to reject)";
    reason.setAttribute("aria-label",
      `Reason for proposal ${proposal.id}`);
    const approve = document.createElement("button");
    approve.type = "button";
    approve.textContent = "Approve";
    approve.addEventListener("click", () =>
      reviewProposal(proposal.id, "approve", reason.value));
    const reject = document.createElement("button");
    reject.type = "button";
    reject.textContent = "Reject";
    reject.addEventListener("click", () =>
      reviewProposal(proposal.id, "reject", reason.value));
    item.append(title, note, load, reason, approve, reject);
    list.appendChild(item);
  }
}

async function reviewProposal(id, decision, reason) {
  const response = await authedFetch(`/api/proposals/${id}/review`, {
    method: "POST",
    headers: headers(),
    body: JSON.stringify({ decision: decision, reason: reason }),
  });
  if (response === null) return;
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    statusLine(`Review failed (${response.status}): ${body.error || ""}`);
    return;
  }
  if (decision === "approve" && body.proposal.page_id === state.pageId &&
      state.pageId) {
    statusLine(
      `Approved as region ${body.proposal.region_index}. Reloading page.`,
    );
    await loadPageByRef(state.pageRef);
  } else {
    statusLine(`Proposal #${id} ${decision}d.`);
  }
  await loadProposals();
  await refreshSession();
}

async function loadCoverageQueue() {
  const list = document.getElementById("coverage-list");
  list.innerHTML = "";
  const response = await authedFetch("/api/coverage/pages", {
    headers: headers(),
  });
  if (response === null) return;
  if (!response.ok) {
    statusLine(`Coverage queue failed (${response.status}).`);
    return;
  }
  const body = await response.json();
  state.coverageQueue = body.pages || [];
  if (state.coverageQueue.length === 0) {
    list.textContent = "No pages.";
    return;
  }
  for (const entry of state.coverageQueue) {
    const item = document.createElement("div");
    item.className = "proposal-item";
    const title = document.createElement("div");
    title.className = "mono";
    title.textContent = `${entry.alias} · ${entry.regions} regions · ` +
      `${entry.pending_proposals} pending` +
      (entry.census_approved
        ? ` · signed off by ${entry.census_reviewer}` : "");
    const load = document.createElement("button");
    load.type = "button";
    load.textContent = "Open page";
    load.addEventListener("click", () => {
      openCoveragePage(entry.alias);
    });
    item.append(title, load);
    list.appendChild(item);
  }
}

async function openCoveragePage(alias) {
  await loadPageByRef(alias);
  if (!state.pageId) return;
  const entry = state.coverageQueue.find(
    (item) => item.page_id === state.pageId) || null;
  state.coverageSignoff = entry;
  const row = document.getElementById("signoff-row");
  row.hidden = false;
  renderSignoff();
  await loadProposals();
  alignRegionToPanel();
}

function renderSignoff() {
  const entry = state.coverageSignoff;
  const button = document.getElementById("signoff-toggle");
  const label = document.getElementById("signoff-state");
  if (!entry) {
    button.disabled = true;
    label.textContent = "";
    return;
  }
  button.disabled = false;
  button.textContent = entry.census_approved
    ? "Withdraw sign-off"
    : "Sign off census";
  label.textContent = entry.census_approved
    ? `Checked by ${entry.census_reviewer}.`
    : "Not yet checked.";
}

async function toggleSignoff() {
  const entry = state.coverageSignoff;
  if (!entry) return;
  const response = await authedFetch(
    `/api/coverage/pages/${encodeURIComponent(entry.alias)}/signoff`,
    {
      method: "POST",
      headers: headers(),
      body: JSON.stringify({ approved: !entry.census_approved }),
    },
  );
  if (response === null) return;
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    statusLine(`Sign-off failed (${response.status}): ${body.error || ""}`);
    return;
  }
  state.coverageSignoff = Object.assign({}, entry, {
    census_approved: body.signoff.census_approved,
    census_reviewer: body.signoff.census_reviewer,
  });
  renderSignoff();
  await loadCoverageQueue();
  statusLine(body.signoff.census_approved
    ? "Census signed off."
    : "Sign-off withdrawn.");
}

function logout() {
  showLogin("Logged out.");
}

document.getElementById("login").addEventListener("click", login);
document.getElementById("logout").addEventListener("click", logout);
document.getElementById("load").addEventListener("click", () => {
  const picker = document.getElementById("page-picker");
  loadPageByRef(picker.value);
});
document.getElementById("prev-region").addEventListener("click", () => {
  stashActiveText();
  if (state.activePos > 0) {
    state.activePos -= 1;
    showCrop();
    renderStepper();
  }
});
document.getElementById("next-region").addEventListener("click", () => {
  stashActiveText();
  if (state.activePos < state.order.length - 1) {
    state.activePos += 1;
    showCrop();
    renderStepper();
  }
});
document.getElementById("save-next").addEventListener("click", saveAndNext);
document.getElementById("save-exit").addEventListener("click", async () => {
  const result = await saveDraft(false);
  if (result !== null) renderStepper();
});
document.getElementById("submit-page").addEventListener("click", submitPage);
document.getElementById("region-text").addEventListener("input", () => {
  stashActiveText();
  const total = state.order.length;
  const done = Object.keys(state.texts).length;
  document.getElementById("draft-count").textContent =
    `${done} of ${total} regions have text.`;
});
document.getElementById("report-toggle").addEventListener("click", () => {
  setReportMode(!state.reportMode);
});
document.getElementById("proposal-confirm").addEventListener("click",
  sendProposal);
document.getElementById("proposal-cancel").addEventListener("click", () => {
  state.pendingGeom = null;
  document.getElementById("proposal-form").hidden = true;
  statusLine("Proposal cancelled.");
});
document.getElementById("zoom-toggle").addEventListener("click", () => {
  const zoom = document.getElementById("zoom-scroll");
  setZoom(!zoom.classList.contains("zoomed"));
});
document.getElementById("load-proposals").addEventListener("click",
  loadProposals);
document.getElementById("load-coverage").addEventListener("click",
  loadCoverageQueue);
document.getElementById("signoff-toggle").addEventListener("click",
  toggleSignoff);
bindDrag("overlay", fullToPageGeometry);
bindDrag("crop-overlay", cropToPageGeometry);
bindZoomTap();
try {
  state.token = window.localStorage.getItem(TOKEN_KEY) || "";
} catch (err) {
  state.token = "";
}
if (state.token) {
  enterApp();
} else {
  refreshSession();
}
