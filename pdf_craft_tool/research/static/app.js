/* Blind annotation client: only the annotator's own blind endpoints. */
"use strict";

const state = { token: "", pageId: "", revision: 0 };

function headers() {
  return {
    "Content-Type": "application/json",
    "X-Annotation-Token": state.token,
  };
}

async function refreshSession() {
  const response = await fetch("/api/session", { headers: headers() });
  if (!response.ok) return;
  const session = await response.json();
  const progress = session.progress || {};
  const line = `pages: ${progress.pages ?? "?"} | ` +
    `submitted: ${progress.submitted_assignments ?? "?"} | ` +
    `open conflicts: ${progress.open_conflicts ?? "?"}`;
  document.getElementById("progress").textContent = line;
}

async function loadPage() {
  const status = document.getElementById("status");
  state.token = document.getElementById("token").value.trim();
  state.pageId = document.getElementById("page-id").value.trim();
  if (!state.token || !state.pageId) {
    status.textContent = "Paste your token and a page id first.";
    return;
  }
  status.textContent = "Loading blind payload…";
  const response = await fetch(
    `/api/page/${encodeURIComponent(state.pageId)}`,
    { headers: headers() },
  );
  if (!response.ok) {
    status.textContent = `Load failed (${response.status}).`;
    return;
  }
  const payload = await response.json();
  state.revision = payload.revision;
  document.getElementById("image-hash").textContent =
    `frozen image sha256: ${payload.image_sha256 || ""}`;
  const image = document.getElementById("page-image");
  const missing = document.getElementById("image-missing");
  const imageUrl = `/api/page/${encodeURIComponent(state.pageId)}/image` +
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
  const lines = document.getElementById("lines");
  lines.innerHTML = "";
  for (const slot of payload.line_slots || []) {
    const wrapper = document.createElement("div");
    wrapper.className = "line-slot";
    const label = document.createElement("label");
    label.textContent = `Region ${slot.region_index}`;
    const area = document.createElement("textarea");
    area.name = `region-${slot.region_index}`;
    area.dataset.region = String(slot.region_index);
    area.setAttribute("aria-label", `Transcription for region ${slot.region_index}`);
    wrapper.appendChild(label);
    wrapper.appendChild(area);
    lines.appendChild(wrapper);
  }
  document.getElementById("submit").disabled = false;
  status.textContent = `Loaded ${(payload.line_slots || []).length} line slots.`;
  await refreshSession();
}

async function submitForm(event) {
  event.preventDefault();
  const status = document.getElementById("status");
  const lines = {};
  for (const area of document.querySelectorAll("#lines textarea")) {
    lines[area.dataset.region] = area.value;
  }
  const response = await fetch(
    `/api/page/${encodeURIComponent(state.pageId)}/submit`,
    {
      method: "POST",
      headers: headers(),
      body: JSON.stringify({ revision: state.revision, lines: lines }),
    },
  );
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    status.textContent = `Submit failed (${response.status}): ${body.error || ""}`;
    return;
  }
  state.revision = body.assignment.revision;
  status.textContent = "Submitted. Thank you — the next page can be loaded.";
  await refreshSession();
}

document.getElementById("load").addEventListener("click", loadPage);
document.getElementById("lines-form").addEventListener("submit", submitForm);
refreshSession();
