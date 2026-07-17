const form = document.getElementById('loginForm');
const errorBox = document.getElementById('loginError');
const submitBtn = document.getElementById('loginSubmit');
const totpInput = document.getElementById('loginTotp');

function showError(msg) {
  errorBox.textContent = msg;
  errorBox.style.display = 'block';
}

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  errorBox.style.display = 'none';
  submitBtn.disabled = true;
  submitBtn.textContent = 'Anmelden...';

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
      // Passwort war korrekt, es fehlt nur noch der 2FA-Code
      showError('Bitte 2FA-Code aus der Authenticator-App eingeben.');
      totpInput.focus();
    } else if (res.status === 429) {
      showError('Zu viele Fehlversuche. Bitte kurz warten und erneut versuchen.');
    } else {
      showError('Benutzername, Passwort oder 2FA-Code falsch.');
    }
  } catch (_err) {
    showError('Server nicht erreichbar. Bitte erneut versuchen.');
  } finally {
    submitBtn.disabled = false;
    submitBtn.textContent = 'Anmelden';
  }
});
