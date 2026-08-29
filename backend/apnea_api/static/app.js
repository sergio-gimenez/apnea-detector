const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value) => String(value).replace(/[&<>'"]/g, character => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[character]));
let currentSession = null;
let currentEvents = [];

const duration = (seconds) => {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  return h ? `${h}h ${m}m` : `${m}m ${s}s`;
};
const clock = (seconds) => new Date(currentSession.started_at_utc).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit', timeZone:'UTC'}) + ` +${duration(seconds)}`;
let apiToken = sessionStorage.getItem('apnea-api-token') || '';
const api = async (url, options = {}, retried = false) => {
  options.headers = {...(options.headers || {}), ...(apiToken ? {Authorization:`Bearer ${apiToken}`} : {})};
  const response = await fetch(url, options);
  if (response.status === 401 && !retried) {
    apiToken = prompt('Prototype API token') || '';
    if (apiToken) sessionStorage.setItem('apnea-api-token', apiToken);
    return api(url, options, true);
  }
  if (!response.ok) throw new Error((await response.json()).detail || response.statusText);
  return response.json();
};
const announce = (message = '') => { $('#notice').textContent = message; };

async function loadSessions() {
  const sessions = await api('/api/sessions');
  $('#sessions').innerHTML = sessions.length ? sessions.map(session => `
    <button class="session" data-id="${session.id}">
      <small>${session.status.toUpperCase()} / ${session.id.slice(0,8)}</small>
      <strong>${new Date(session.started_at_utc).toLocaleString()}</strong>
      <small>${duration(session.duration_seconds)} · ${escapeHtml(session.device_id)}</small>
    </button>`).join('') : '<p class="muted">No uploads yet. Start an Android capture.</p>';
  document.querySelectorAll('.session').forEach(button => button.onclick = () => openSession(button.dataset.id));
}

async function openSession(id) {
  [currentSession, currentEvents] = await Promise.all([api(`/api/sessions/${id}`), api(`/api/sessions/${id}/events`)]);
  const [summary, signals] = await Promise.all([api(`/api/sessions/${id}/summary`), api(`/api/sessions/${id}/signals`)]);
  $('#session-list').classList.add('hidden');
  $('#review').classList.remove('hidden');
  $('#session-date').textContent = `${new Date(currentSession.started_at_utc).toLocaleString()} / ${currentSession.id.slice(0,8)}`;
  const oximetry = summary.oximetry || {};
  $('#metrics').innerHTML = [
    ['SREI', summary.srei ?? '—'], ['Candidates', summary.suspected_events], ['Analyzed', duration(currentSession.duration_seconds)],
    ['With desat', summary.correlated_events ?? 0],
    ['ODI3 (est.)', summary.odi3 ?? '—'], ['ODI4 (est.)', summary.odi4 ?? '—'],
    ['T90', oximetry.t90_seconds ? duration(oximetry.t90_seconds) : '—'],
    ['Min SpO₂', summary.minimum_spo2 == null ? '—' : `${summary.minimum_spo2}%`], ['Mean SpO₂', summary.mean_spo2 == null ? '—' : `${summary.mean_spo2}%`],
    ['SpO₂ coverage', summary.spo2_coverage_hours ? `${summary.spo2_coverage_hours.toFixed(1)}h` : '—']
  ].map(([label,value]) => `<div class="metric"><b>${value}</b><span>${label}</span></div>`).join('');
  renderEvents();
  drawTimeline(signals, currentEvents, oximetry.events || []);
}

function renderEvents() {
  $('#events').innerHTML = currentEvents.length ? currentEvents.map(event => `
    <div class="event" data-id="${event.id}">
      <b>+${duration(event.start_offset_seconds)}</b>
      <span>${event.duration_seconds.toFixed(0)} sec</span>
      <div><div class="confidence"><i style="width:${event.confidence*100}%"></i></div><small>${Math.round(event.confidence*100)}% confidence</small></div>
      <span class="tag">${event.review_status}</span>
    </div>`).join('') : '<p class="muted">No ≥10-second low-audio candidates detected.</p>';
  document.querySelectorAll('.event').forEach(row => row.onclick = () => selectEvent(Number(row.dataset.id)));
}

async function selectEvent(id) {
  const event = currentEvents.find(item => item.id === id);
  const evidence = Object.entries(event.evidence).map(([key,value]) => `<span>${key.replaceAll('_',' ')}</span><span>${value ?? 'n/a'}</span>`).join('');
  $('#evidence').innerHTML = `
    <p class="eyebrow">SUSPECTED RESPIRATORY EVENT</p><h2>${clock(event.start_offset_seconds)}</h2>
    <div class="evidence-list">${evidence}</div>
    <audio id="event-audio" controls preload="none"></audio>
    <p class="hint">30 seconds before and after candidate.</p>
    <div class="review-buttons">${['confirmed','rejected','uncertain'].map(status => `<button data-review="${status}">${status}</button>`).join('')}</div>`;
  document.querySelectorAll('[data-review]').forEach(button => button.onclick = async () => {
    const updated = await api(`/api/events/${event.id}/review`, {method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({status:button.dataset.review})});
    currentEvents[currentEvents.findIndex(item => item.id === id)] = updated;
    renderEvents(); selectEvent(id);
  });
  const audioResponse = await fetch(`/api/events/${event.id}/audio.wav`, {
    headers: apiToken ? {Authorization:`Bearer ${apiToken}`} : {},
  });
  if (audioResponse.ok) $('#event-audio').src = URL.createObjectURL(await audioResponse.blob());
}

function drawTimeline(signals, events, desaturations = []) {
  const canvas = $('#timeline');
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth, height = canvas.clientHeight;
  canvas.width = width * ratio; canvas.height = height * ratio;
  const ctx = canvas.getContext('2d'); ctx.scale(ratio, ratio);
  ctx.clearRect(0,0,width,height); ctx.font = '10px DM Mono';
  const start = new Date(currentSession.started_at_utc).getTime();
  const total = Math.max(currentSession.duration_seconds, 1);
  const groups = {audio_energy:[], spo2:[], heart_rate:[], respiration_rate:[]};
  signals.forEach(point => { if (groups[point.signal_type]) groups[point.signal_type].push([(new Date(point.timestamp_utc).getTime()-start)/1000, point.value]); });
  for (let i=0;i<=8;i++) { const x=45+(width-65)*i/8; ctx.strokeStyle='#1f2930';ctx.beginPath();ctx.moveTo(x,20);ctx.lineTo(x,height-30);ctx.stroke();ctx.fillStyle='#65727b';ctx.fillText(duration(total*i/8),x-12,height-10); }
  desaturations.forEach(event => { const x=45+(width-65)*event.start_offset_seconds/total; const w=Math.max(2,(width-65)*event.duration_seconds/total);ctx.fillStyle='rgba(240,173,78,.16)';ctx.fillRect(x,20,w,115); });
  events.forEach(event => { const x=45+(width-65)*event.start_offset_seconds/total; const w=Math.max(3,(width-65)*event.duration_seconds/total);ctx.fillStyle='rgba(171,128,255,.22)';ctx.fillRect(x,20,w,height-50); });
  const draw = (points,color,min,max,top,bottom) => { if (!points.length) return;ctx.strokeStyle=color;ctx.lineWidth=1.4;ctx.beginPath();points.forEach(([time,value],i)=>{const x=45+(width-65)*time/total;const y=top+(bottom-top)*(1-(value-min)/(max-min));i?ctx.lineTo(x,y):ctx.moveTo(x,y)});ctx.stroke(); };
  draw(groups.audio_energy,'#58d6d0',-80,-10,28,height-38); draw(groups.spo2,'#f0ad4e',80,100,28,135); draw(groups.heart_rate,'#ff6b62',35,120,155,height-38); draw(groups.respiration_rate,'#88d498',6,30,155,height-38);
}

$('#back').onclick = () => { $('#review').classList.add('hidden');$('#session-list').classList.remove('hidden');announce();loadSessions(); };
$('#reanalyze').onclick = async () => { try { announce('Analyzing audio and correlating physiology…');await api(`/api/sessions/${currentSession.id}/analyze`,{method:'POST'});announce('Analysis complete.');await openSession(currentSession.id); } catch(error){announce(error.message)} };
$('#garmin').onclick = async () => { try { announce('Fetching Garmin sleep and health signals…');const result=await api(`/api/sessions/${currentSession.id}/garmin/import`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});announce(`Imported ${result.imported} Garmin points. Re-run analysis to fuse them.`);await openSession(currentSession.id); } catch(error){announce(error.message)} };
window.addEventListener('resize', () => currentSession && openSession(currentSession.id));
loadSessions().catch(error => announce(error.message));
