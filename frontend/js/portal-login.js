// js/portal-login.js
//
// The submit behaviour shared by every official portal (authority, contractor,
// admin). Each portal owns its own look; none of them owns a second copy of
// this logic, which is what previously let three near-identical pages drift.
//
// The fence itself is not here. `login()` sends `expected_role` and the server
// decides - this file only reports the refusal.

function mountPortalLogin({ uiRole, dashboard, formId = "login-form" }) {
  // Already signed in on this portal? Skip the form.
  if (TokenStore.isSignedIn() && localStorage.getItem("bn_role") === uiRole) {
    window.location.replace(dashboard);
    return;
  }

  const form = document.getElementById(formId);
  const submitBtn = form.querySelector("button[type=submit]");
  const errorBox = document.getElementById("form-error");

  function showError(message) {
    if (!errorBox) {
      showToast(message, "error");
      return;
    }
    errorBox.textContent = message;
    errorBox.classList.remove("hidden");
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (errorBox) errorBox.classList.add("hidden");

    const original = submitBtn.textContent;
    submitBtn.disabled = true;
    submitBtn.textContent = "Verifying…";

    try {
      await login(
        document.getElementById("email").value.trim(),
        document.getElementById("password").value,
        uiRole
      );
      sessionStorage.setItem("bn_flash", "Signed in successfully");
      window.location.href = dashboard;
    } catch (error) {
      // reportApiError already toasts; the inline box is what keeps the
      // refusal on screen next to the form the user is staring at.
      showError(reportApiError(error, "Could not sign in."));
    } finally {
      submitBtn.disabled = false;
      submitBtn.textContent = original;
    }
  });
}
