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
    button.textContent = form.action.endsWith('telegram-test') ? 'Sending…' : 'Saving…';
    feedback.className = 'hint';
    feedback.textContent = form.action.endsWith('telegram-test') ? 'Sending test message…' : 'Updating connection…';
    try {
      const response = await fetch(form.action, { method: 'POST', body: new URLSearchParams(new FormData(form)) });
      if (!response.ok) {
        const doc = new DOMParser().parseFromString(await response.text(), 'text/html');
        throw Error(doc.querySelector('.error')?.textContent || 'The connection could not be updated. Check your details and try again.');
      }
      if (form.action.endsWith('telegram-test')) {
        feedback.textContent = 'Test message sent. Check your Telegram chat. Mic check: one, two.';
      } else {
        dirty = false;
        location.href = '/profile';
      }
    } catch (error) {
      feedback.className = 'error';
      feedback.textContent = error instanceof TypeError ? 'Connection lost. Check your settings before trying again.' : error.message;
      feedback.setAttribute('tabindex', '-1');
      feedback.focus();
    } finally {
      button.disabled = false;
      button.textContent = text;
    }
  });
}
