const form = document.getElementById('loginForm');
const errorBox = document.getElementById('loginError');
const submitBtn = document.getElementById('loginSubmit');
const totpField = document.getElementById('loginTotpField');
const totpInput = document.getElementById('loginTotp');

function showError(msg) {
  errorBox.textContent = msg;
  errorBox.style.display = 'block';
}

// Self-check on load: catches the most common silent failure (server
// requires Secure cookies but the page loaded over plain HTTP, so the
// browser drops the cookie without saying so - login returns 200 but
// no session ever exists).
(async () => {
  try {
    const res = await fetch('/api/config', { credentials: 'include' });
    if (!res.ok) return;
    const cfg = await res.json();

    if (cfg.cookie_secure && window.location.protocol !== 'https:') {
      showError(
        'Warning: the server requires secure cookies (COOKIE_SECURE=true), ' +
        'but this page is loaded over plain HTTP. Login will fail silently. ' +
        'Either access it over HTTPS, or set COOKIE_SECURE=false in .env ' +
        'and restart the container.'
      );
    }

    if (!cfg.totp_required) {
      totpField.style.display = 'none';
    } else {
      totpInput.setAttribute('required', 'required');
    }
  } catch (_err) {
    // /api/config unreachable - not a blocker, form stays usable normally
  }
})();

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  errorBox.style.display = 'none';
  submitBtn.disabled = true;
  submitBtn.textContent = 'Signing in...';

  const username = document.getElementById('loginUser').value;
  const password = document.getElementById('loginPass').value;
  const totp_code = totpInput.value.trim() || undefined;

  try {
    const res = await fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password, totp_code }),
      credentials: 'include',
    });

    if (res.ok) {
      const params = new URLSearchParams(window.location.search);
      window.location.href = params.get('next') || '/';
      return;
    }

    if (res.status === 400) {
      // Password was correct, just missing the 2FA code
      showError('Please enter the 2FA code from your authenticator app.');
      totpInput.focus();
    } else if (res.status === 429) {
      showError('Too many failed attempts. Please wait a moment and try again.');
    } else {
      showError('Username, password, or 2FA code is incorrect.');
    }
  } catch (_err) {
    showError('Server unreachable. Please try again.');
  } finally {
    submitBtn.disabled = false;
    submitBtn.textContent = 'Sign In';
  }
});
