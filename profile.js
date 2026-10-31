const settingsForm = document.querySelector('.settings-form');
let dirty = false;
settingsForm.addEventListener('input', () => { dirty = true; });
window.addEventListener('beforeunload', event => {
  if (dirty) { event.preventDefault(); event.returnValue = ''; }
});
for (const form of document.querySelectorAll('.profile-page form')) {
  form.addEventListener('submit', async event => {
    if (event.defaultPrevented || form.action.endsWith('/spotify/connect')) return;
    event.preventDefault();
    const button = form.querySelector('button');
    if (button.disabled) return;
    const text = button.textContent;
    const feedback = document.getElementById('settings-feedback');
    button.disabled = true;
    button.textContent = form.action.endsWith('telegram-test') ? 'Versturen…' : 'Opslaan…';
    feedback.className = 'hint';
    feedback.textContent = form.action.endsWith('telegram-test') ? 'Testbericht versturen…' : 'Koppeling bijwerken…';
    try {
      const response = await fetch(form.action, { method: 'POST', body: new URLSearchParams(new FormData(form)) });
      if (!response.ok) {
        const doc = new DOMParser().parseFromString(await response.text(), 'text/html');
        throw Error(doc.querySelector('.error')?.textContent || 'De koppeling kon niet worden bijgewerkt. Controleer je gegevens en probeer het opnieuw.');
      }
      if (form.action.endsWith('telegram-test')) {
        feedback.textContent = 'Testbericht verstuurd. Kijk in je Telegram-chat. Een melding die je zelf hebt aangevraagd.';
      } else {
        dirty = false;
        location.href = '/profile';
      }
    } catch (error) {
      feedback.className = 'error';
      feedback.textContent = error instanceof TypeError ? 'Verbinding verbroken. Controleer je instellingen voordat je het opnieuw probeert.' : error.message;
      feedback.setAttribute('tabindex', '-1');
      feedback.focus();
    } finally {
      button.disabled = false;
      button.textContent = text;
    }
  });
}
