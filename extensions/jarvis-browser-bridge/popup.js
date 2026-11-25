const statusEl = document.getElementById("status");
const pairInput = document.getElementById("pair-code");

function setStatus(text) {
  statusEl.textContent = text || "";
}

function send(type, extra) {
  const message = Object.assign({ type: type }, extra || {});
  return chrome.runtime.sendMessage(message);
}

document.getElementById("connect").addEventListener("click", async () => {
  const pairCode = String(pairInput.value || "").trim().toUpperCase();
  setStatus("Connecting…");
  const reply = await send("handshake", { pair_code: pairCode });
  if (reply && reply.ok) {
    setStatus("Connected. Actions still need approval in JARVIS.");
    return;
  }
  setStatus("Connection failed: " + ((reply && reply.error) || "unknown"));
});

document.getElementById("inspect").addEventListener("click", async () => {
  setStatus("Sending active tab…");
  const reply = await send("inspect");
  if (reply && reply.ok) {
    setStatus("Sent. Approve the inspection in JARVIS.");
    return;
  }
  setStatus("Inspect failed: " + ((reply && reply.error) || "unknown"));
});

document.getElementById("read").addEventListener("click", async () => {
  setStatus("Reading active page…");
  const reply = await send("read");
  if (reply && reply.ok) {
    setStatus("Sent. Approve the page read in JARVIS.");
    return;
  }
  setStatus("Read failed: " + ((reply && reply.error) || "unknown"));
});

document.getElementById("scan").addEventListener("click", async () => {
  setStatus("Scanning targets…");
  const reply = await send("scan_targets");
  if (reply && reply.ok) {
    setStatus("Scanned " + String(reply.count || 0) + " target(s). Propose an action in JARVIS.");
    return;
  }
  setStatus("Scan failed: " + ((reply && reply.error) || "unknown"));
});

document.getElementById("run").addEventListener("click", async () => {
  setStatus("Running approved action…");
  const reply = await send("run_approved");
  if (reply && reply.ok) {
    setStatus("Action finished.");
    return;
  }
  setStatus("Run failed: " + ((reply && reply.error) || "unknown"));
});

document.getElementById("disconnect").addEventListener("click", async () => {
  const reply = await send("disconnect");
  setStatus(reply && reply.ok ? "Disconnected." : "Disconnect failed.");
});
