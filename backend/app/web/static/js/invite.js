"use strict";

// External file, not an inline <script>: app/main.py's CSP is script-src
// 'self' without 'unsafe-inline'. Visibility is toggled with the .hidden class
// for the same reason (style-src 'self' blocks inline style attributes).
(function () {
  const params = new URLSearchParams(window.location.search);
  const token = params.get("token");
  const email = params.get("email") || "";

  // The token must not linger in the address bar, history or later Referer
  // headers once it has been read.
  window.history.replaceState(null, "", window.location.pathname);

  const show = (id) => document.getElementById(id).classList.remove("hidden");
  const hide = (id) => document.getElementById(id).classList.add("hidden");

  document.getElementById("email-field").value = email;

  if (!token) {
    show("missing-token");
    hide("accept-form");
    hide("intro");
    return;
  }

  document.getElementById("accept-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const errorEl = document.getElementById("form-error");
    hide("form-error");

    const password = document.getElementById("password").value;
    const password2 = document.getElementById("password2").value;
    if (password !== password2) {
      errorEl.textContent = "Passwords do not match.";
      show("form-error");
      return;
    }

    const displayName = document.getElementById("display-name").value.trim();
    const submitBtn = e.target.querySelector("button[type=submit]");
    submitBtn.disabled = true;
    try {
      const resp = await fetch("/api/auth/register", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: email,
          password: password,
          display_name: displayName || null,
          invite_token: token,
        }),
      });
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        throw new Error(body.detail || body.message || `Request failed (${resp.status}).`);
      }
      // Registration also opens a browser session that nothing here uses;
      // end it so no credential is left behind on this device.
      await fetch("/api/auth/logout", { method: "POST" }).catch(() => {});
      hide("accept-form");
      hide("intro");
      show("success");
    } catch (err) {
      errorEl.textContent = err.message;
      show("form-error");
    } finally {
      submitBtn.disabled = false;
    }
  });
})();
