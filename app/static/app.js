const $ = (id) => document.getElementById(id);
const dateFormat = new Intl.DateTimeFormat('en-AU', {
  timeZone: 'Australia/Melbourne', weekday: 'short', hour: 'numeric', minute: '2-digit',
});
const logFormat = new Intl.DateTimeFormat('en-AU', {
  timeZone: 'Australia/Melbourne', day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit',
});

function summary() {
  const count = document.querySelectorAll('.day input:checked').length;
  $('schedule-summary').textContent = count
    ? `${$('start_time').value} · ${$('duration').value} minutes · ${count === 7 ? 'every day' : count + ' days a week'}. Save changes to apply.`
    : 'Choose at least one day.';
}
$('schedule-form').addEventListener('input', summary);
summary();

async function refresh() {
  try {
    const response = await fetch('/api/status', {cache: 'no-store'});
    if (response.status === 401) {
      window.location.assign('/login');
      return;
    }
    if (!response.ok) throw new Error('Status unavailable');
    const state = await response.json();
    $('poll-error').hidden = true;
    $('heater-mode').textContent = state.heater.mode;
    $('heater-detail').textContent = state.heater.detail;
    $('schedule-tag').textContent = state.schedule.enabled ? 'Schedule on' : 'Schedule paused';
    $('local-clock').textContent = state.local_time;
    const active = state.active;
    $('next-label').textContent = active ? 'BOOST WINDOW ENDS' : 'NEXT SCHEDULED BOOST';
    $('next-start').textContent = active ? dateFormat.format(new Date(active.ends_at))
      : state.next_start ? dateFormat.format(new Date(state.next_start)) : 'No boost scheduled';
    $('next-detail').textContent = active ? active.message
      : state.schedule.enabled ? `${state.schedule.duration} minutes · Melbourne time` : 'Enable your schedule to get started';
    $('active-message').textContent = active ? active.message : 'No active boost window.';
    $('active-message').classList.toggle('is-active', Boolean(active));
    $('boost-button').disabled = Boolean(active) || !state.cloud_signed_in;
    $('stop-button').disabled = !active;
    if ($('logout-button')) $('logout-button').textContent = active ? 'End boost & sign out' : 'Sign out';
    if ($('saved-login-status')) {
      $('saved-login-status').textContent = state.saved_login
        ? 'Encrypted login saved. Automatic re-login enabled.'
        : 'Saving disabled. Current session only.';
      $('forget-form').hidden = !state.saved_login;
      $('auth-error').textContent = state.auth_error || (state.cloud_signed_in ? '' : 'Emerald sign-in is required before heating can run.');
    }
    const rows = state.history.map((event) => {
      const row = document.createElement('div');
      row.className = 'activity-row';
      const marker = document.createElement('span');
      marker.className = 'activity-dot';
      const message = document.createElement('p');
      message.textContent = event.message;
      const timestamp = document.createElement('time');
      timestamp.dateTime = event.created_at;
      timestamp.textContent = logFormat.format(new Date(event.created_at));
      row.append(marker, message, timestamp);
      return row;
    });
    if (rows.length) $('activity-list').replaceChildren(...rows);
    if (state.error) {
      $('poll-error').textContent = state.error;
      $('poll-error').hidden = false;
    }
  } catch {
    $('poll-error').textContent = 'Connection to controller lost. Display may be out of date. Reconnecting…';
    $('poll-error').hidden = false;
  }
}
refresh();
setInterval(refresh, 10000);
