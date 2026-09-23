"use strict";

// Kept as an external file, not an inline <script>, because app/main.py's
// CSP is script-src 'self' with no 'unsafe-inline' -- an inline script on
// this page would be silently blocked by any CSP-respecting browser,
// breaking the redemption flow it exists to power.
(function () {
  const params = new URLSearchParams(window.location.search);
  const token = params.get("token");
  const email = params.get("email") || "";

  document.getElementById("email-field").value = email;

  if (!token) {
    document.getElementById("missing-token").style.display = "block";
    document.getElementById("accept-form").style.display = "none";
    document.getElementById("intro").style.display = "none";
    return;
  }

  document.getElementById("accept-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const errorEl = document.getElementById("form-error");
    errorEl.style.display = "none";

    const password = document.getElementById("password").value;
    const password2 = document.getElementById("password2").value;
    if (password !== password2) {
      errorEl.textContent = "Passwords do not match.";
      errorEl.style.display = "block";
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
      // A successful registration also sets a browser session cookie for
      // this page's own origin -- not useful here (there is no technician
      // PWA to land in), so it's left alone rather than built out into a
      // second, redundant sign-in surface.
      document.getElementById("accept-form").style.display = "none";
      document.getElementById("intro").style.display = "none";
      document.getElementById("success").style.display = "block";
    } catch (err) {
      errorEl.textContent = err.message;
      errorEl.style.display = "block";
    } finally {
      submitBtn.disabled = false;
    }
  });
})();
