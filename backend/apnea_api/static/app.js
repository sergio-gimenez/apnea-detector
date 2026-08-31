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

/* ---------- wall-clock time ----------
   start_offset_seconds counts from currentSession.started_at_utc, which the phone
   writes with Instant.now(): a real UTC instant, unaffected by whatever timezone
   the phone's clock is set to. So a clip's wall-clock time is just that UTC
   instant shown in `zone` — the reviewer's own timezone by default, and
   overridable (the TZ button) for when that guess is wrong. */
const TZ_KEY = 'apnea-review-tz';
let zone = 'UTC';
try {
  zone = localStorage.getItem(TZ_KEY) || Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
} catch { zone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'; }

const shortZone = () => {
  try {
    const part = new Intl.DateTimeFormat('en-GB', {timeZone: zone, timeZoneName: 'shortOffset'})
      .formatToParts(new Date()).find(item => item.type === 'timeZoneName');
    return part ? part.value.replace('GMT', 'UTC') : zone;
  } catch { return zone; }
};
const zoneLabel = () => `${zone} (${shortZone()})`;
const atOffset = (seconds) => new Date(Date.parse(currentSession.started_at_utc) + seconds * 1000);
const wallTime = (date, withSeconds = true) => date.toLocaleTimeString('en-GB',
  {hour: '2-digit', minute: '2-digit', ...(withSeconds ? {second: '2-digit'} : {}), timeZone: zone});
const wallDay = (date) => date.toLocaleDateString('en-GB',
  {weekday: 'short', day: '2-digit', month: 'short', timeZone: zone});
// only worth showing a date once a clip crosses into a different calendar day
const dayPrefix = (date) => wallDay(date) === wallDay(atOffset(0)) ? '' : `${wallDay(date)} `;
const clockRange = (start, span) => {
  const from = atOffset(start), to = atOffset(start + span);
  return `${dayPrefix(from)}${wallTime(from)} – ${wallTime(to)}`;
};
const clock = (seconds) => `${dayPrefix(atOffset(seconds))}${wallTime(atOffset(seconds))} (+${duration(seconds)})`;
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
  $('#session-date').textContent = `${new Date(currentSession.started_at_utc).toLocaleString('en-GB', {timeZone: zone})} · ${zoneLabel()} · ${currentSession.id.slice(0,8)}`;
  $('#tz').textContent = `🕓 ${shortZone()}`;
  const oximetry = summary.oximetry || {};
  const arch = summary.sleep_architecture;
  $('#algorithm').textContent = summary.algorithm_version || '';
  $('#architecture').innerHTML = !arch ? '' : `
    <p class="eyebrow">WEARABLE SLEEP ARCHITECTURE · ${arch.calendar_date}</p>
    <div class="arch">
      ${[['Score', arch.sleep_score ?? '—'], ['Asleep', arch.sleep_hours ? arch.sleep_hours + 'h' : '—'],
         ['Deep', arch.deep_percent == null ? '—' : arch.deep_percent + '%'],
         ['Light', arch.light_percent == null ? '—' : arch.light_percent + '%'],
         ['REM', arch.rem_percent == null ? '—' : arch.rem_percent + '%'],
         ['Awakenings', arch.awake_count ?? '—'], ['Restless', arch.restless_moments ?? '—']]
        .map(([label, value]) => `<div><b>${value}</b><span>${label}</span></div>`).join('')}
    </div>
    <p class="hint">${escapeHtml(arch.note || '')}</p>`;
  $('#metrics').innerHTML = [
    ['SREI', summary.srei ?? '—'], ['Candidates', summary.suspected_events], ['Analyzed', duration(currentSession.duration_seconds)],
    ['With desat', summary.correlated_events ?? 0],
    ['ODI3 (est.)', summary.odi3 ?? '—'], ['ODI4 (est.)', summary.odi4 ?? '—'],
    ['T90', oximetry.t90_seconds ? duration(oximetry.t90_seconds) : '—'],
    ['Min SpO₂', summary.minimum_spo2 == null ? '—' : `${summary.minimum_spo2}%`], ['Mean SpO₂', summary.mean_spo2 == null ? '—' : `${summary.mean_spo2}%`],
    ['SpO₂ coverage', summary.spo2_coverage_hours ? `${summary.spo2_coverage_hours.toFixed(1)}h` : '—'],
    ['Snoring', summary.snoring_burden_percent == null ? '—' : `${summary.snoring_burden_percent}%`],
    ['Snore bursts', summary.snore_bursts ?? '—']
  ].map(([label,value]) => `<div class="metric"><b>${value}</b><span>${label}</span></div>`).join('');
  renderEvents();
  drawTimeline(signals, currentEvents, oximetry.events || []);
}

function renderEvents() {
  $('#events').innerHTML = currentEvents.length ? currentEvents.map(event => `
    <div class="event" data-id="${event.id}">
      <b title="+${duration(event.start_offset_seconds)} into the night">${wallTime(atOffset(event.start_offset_seconds), false)}</b>
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
    <p class="hint">Candidate window ${clockRange(event.start_offset_seconds, event.duration_seconds)} · ${zoneLabel()}. Audio includes 30 s before and after.</p>
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
  const groups = {audio_energy:[], spo2:[], heart_rate:[], respiration_rate:[], snore_rate:[]};
  signals.forEach(point => { if (groups[point.signal_type]) groups[point.signal_type].push([(new Date(point.timestamp_utc).getTime()-start)/1000, point.value]); });
  for (let i=0;i<=8;i++) { const x=45+(width-65)*i/8; ctx.strokeStyle='#1f2930';ctx.beginPath();ctx.moveTo(x,20);ctx.lineTo(x,height-30);ctx.stroke();ctx.fillStyle='#65727b';ctx.fillText(wallTime(atOffset(total*i/8),false),x-14,height-10); }
  desaturations.forEach(event => { const x=45+(width-65)*event.start_offset_seconds/total; const w=Math.max(2,(width-65)*event.duration_seconds/total);ctx.fillStyle='rgba(240,173,78,.16)';ctx.fillRect(x,20,w,115); });
  events.forEach(event => { const x=45+(width-65)*event.start_offset_seconds/total; const w=Math.max(3,(width-65)*event.duration_seconds/total);ctx.fillStyle='rgba(171,128,255,.22)';ctx.fillRect(x,20,w,height-50); });
  const draw = (points,color,min,max,top,bottom) => { if (!points.length) return;ctx.strokeStyle=color;ctx.lineWidth=1.4;ctx.beginPath();points.forEach(([time,value],i)=>{const x=45+(width-65)*time/total;const y=top+(bottom-top)*(1-(value-min)/(max-min));i?ctx.lineTo(x,y):ctx.moveTo(x,y)});ctx.stroke(); };
  draw(groups.audio_energy,'#58d6d0',-80,-10,28,height-38); draw(groups.spo2,'#f0ad4e',80,100,28,135); draw(groups.heart_rate,'#ff6b62',35,120,155,height-38); draw(groups.respiration_rate,'#88d498',6,30,155,height-38); draw(groups.snore_rate,'#ab80ff',0,40,155,height-38);
}

$('#back').onclick = () => { $('#review').classList.add('hidden');$('#session-list').classList.remove('hidden');announce();loadSessions(); };
const runAnalysis = async (algorithm) => { try { announce(`Running ${algorithm} over the night…`);const result=await api(`/api/sessions/${currentSession.id}/analyze?algorithm=${encodeURIComponent(algorithm)}`,{method:'POST'});announce(`${result.algorithm_version}: ${result.events} candidates.`);await openSession(currentSession.id); } catch(error){announce(error.message)} };
$('#reanalyze').onclick = () => runAnalysis('dsp-v0.2.0');
$('#garmin').onclick = async () => { try { announce('Fetching Garmin sleep and health signals…');const result=await api(`/api/sessions/${currentSession.id}/garmin/import`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});announce(`Imported ${result.imported} Garmin points. Re-run analysis to fuse them.`);await openSession(currentSession.id); } catch(error){announce(error.message)} };
/* ---------- blinded labelling ---------- */
let batch = [], batchIndex = 0;

const authedBlob = async (url) => {
  const response = await fetch(url, {headers: apiToken ? {Authorization:`Bearer ${apiToken}`} : {}});
  if (!response.ok) throw new Error(`Clip unavailable (${response.status})`);
  return URL.createObjectURL(await response.blob());
};

/* ---------- make quiet clips audible ----------
   Overnight phone audio is quiet and wide-range: snores near 0 dBFS, the breaths
   between them near the noise floor. A gain stage lifts the whole clip past 100 %
   volume; an optional compressor pulls the quiet parts up without the snores
   getting painful. All client-side, the stored audio is untouched. */
const GAIN_KEY = 'apnea-label-gain';
let audioChain = null;

function ensureAudioChain() {
  if (audioChain || audioChain === false) return audioChain;
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    const ctx = new Ctx();
    const source = ctx.createMediaElementSource($('#label-audio'));
    const gain = ctx.createGain();
    const comp = ctx.createDynamicsCompressor();
    comp.threshold.value = -40; comp.knee.value = 25; comp.ratio.value = 6;
    comp.attack.value = 0.004; comp.release.value = 0.18;
    source.connect(gain);
    audioChain = {ctx, gain, comp};
    applyAudioChain();
  } catch (error) {
    audioChain = false; // unsupported: fall back to the native element
  }
  return audioChain;
}

function applyAudioChain() {
  if (!audioChain) return;
  const {ctx, gain, comp} = audioChain;
  try { gain.disconnect(); } catch {}
  try { comp.disconnect(); } catch {}
  if ($('#label-compress').checked) { gain.connect(comp); comp.connect(ctx.destination); }
  else { gain.connect(ctx.destination); }
}

function applyGain() {
  const value = Number($('#label-gain').value);
  $('#label-gain-val').textContent = `${value}×`;
  if (audioChain) audioChain.gain.gain.value = value;
  try { localStorage.setItem(GAIN_KEY, value); } catch {}
}

async function openLabeling(fresh) {
  // the panel always opens: a button that silently does nothing reads as broken
  $('#labeling').classList.remove('hidden');
  $('#labeling').scrollIntoView({behavior:'smooth', block:'start'});
  try {
    batch = fresh ? [] : await api(`/api/sessions/${currentSession.id}/review-batch`);
    if (!batch.length) {
      $('#label-progress').textContent = 'Preparing clips…';
      $('#label-buttons').classList.add('hidden');
      announce('Building a blinded batch…');
      const made = await api(`/api/sessions/${currentSession.id}/review-batch`, {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({control_ratio: 1.0}),
      });
      announce(`${made.items} clips queued: ${made.candidate} candidates and ${made.control} controls, shuffled.`);
      batch = await api(`/api/sessions/${currentSession.id}/review-batch`);
    }
    batchIndex = batch.findIndex(item => !item.labeled);
    if (batchIndex < 0) batchIndex = batch.length;
    await renderClip();
  } catch (error) {
    $('#label-progress').textContent = 'Could not start labelling';
    $('#label-help').textContent = error.message;
    $('#label-buttons').classList.add('hidden');
    announce(error.message);
  }
}

const showClipMedia = (on) => {
  ['#label-wave', '#label-wave-hint', '#label-audio', '.audio-tools']
    .forEach(selector => $(selector).classList.toggle('hidden', !on));
};

async function renderClip() {
  await renderStats();
  $('#label-reveal').classList.add('hidden');
  if (batchIndex >= batch.length) {
    $('#label-progress').textContent = 'All clips labelled';
    $('#label-help').textContent = 'Every clip in this batch has a label. The numbers below are the result.';
    $('#label-buttons').classList.add('hidden');
    showClipMedia(false);
    $('#label-audio').removeAttribute('src');
    waveData = null;
    return;
  }
  const item = batch[batchIndex];
  $('#label-buttons').classList.remove('hidden');
  showClipMedia(true);
  $('#label-progress').textContent = `Clip ${batchIndex + 1} of ${batch.length} · ${wallTime(atOffset(item.start_offset_seconds))}`;
  $('#label-help').textContent = `This clip covers ${clockRange(item.start_offset_seconds, item.duration_seconds)} (${zoneLabel()}), plus 30 s of audio each side. Was there a pause in breathing or snoring? You are not told whether the detector flagged it.`;
  $('#label-wave-hint').textContent = 'Loudness of this clip — click the trace to jump there and listen closely. Whether the detector flagged this clip, and where, stays hidden until you label.';
  const audio = $('#label-audio');
  const [blob, waveform] = await Promise.all([
    authedBlob(`/api/review-items/${item.id}/audio.wav`),
    api(`/api/review-items/${item.id}/waveform`).catch(error => { announce(error.message); return null; }),
  ]);
  audio.src = blob;
  if (waveform) drawWaveform(waveform, false);
}

async function labelClip(label) {
  const item = batch[batchIndex];
  try {
    const revealed = await api(`/api/review-items/${item.id}`, {
      method:'PATCH', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({label}),
    });
    batch[batchIndex] = {...item, ...revealed, labeled:true};
    await reveal(batch[batchIndex], label);
  } catch (error) { announce(error.message); }
}

async function reveal(item, label) {
  const wasFlagged = item.kind === 'candidate';
  const heardPause = label === 'pause';
  const agrees = label !== 'unclear' && wasFlagged === heardPause;
  $('#label-verdict').innerHTML = `
    <span class="badge ${wasFlagged ? 'candidate' : 'control'}">${wasFlagged ? 'DETECTOR FLAGGED THIS' : 'CONTROL WINDOW'}</span>
    <span class="badge">you said ${label.replace('_', ' ')}</span>
    ${label === 'unclear' ? '' : `<span class="badge ${agrees ? 'agree' : 'disagree'}">${agrees ? 'agrees' : (wasFlagged ? 'false positive' : 'MISSED EVENT')}</span>`}
    ${item.event ? `<span class="badge">confidence ${Math.round(item.event.confidence * 100)}%</span>` : ''}
    ${item.event && item.event.evidence.recovery_gasp ? '<span class="badge">recovery gasp</span>' : ''}`;
  $('#label-reveal').classList.remove('hidden');
  $('#label-buttons').classList.add('hidden');
  $('#label-wave-hint').textContent = 'Now with the detector shown: shaded = the window it judged, purple = snore bursts it found, dashed = its burst threshold, amber = SpO₂.';
  try {
    drawWaveform(await api(`/api/review-items/${item.id}/waveform`), true);
  } catch (error) { announce(error.message); }
  await renderStats();
}

let waveData = null;      // last waveform payload, so the playhead can redraw it
let waveAnnotated = false; // whether to draw the detector's own marks (post-label only)
let waveToken = 0;        // invalidates the animation loop of a previous clip

function drawWaveform(data, annotated = false) {
  waveData = data;
  waveAnnotated = annotated;
  const token = ++waveToken;
  renderWave();
  const audio = $('#label-audio');
  const follow = () => renderWave(audio.currentTime);
  audio.ontimeupdate = audio.onseeked = audio.onpause = audio.onended = follow;
  audio.onplay = () => {
    const step = () => {
      if (token !== waveToken || audio.paused || audio.ended) return;
      renderWave(audio.currentTime);
      requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  };
  // click the trace to jump the audio there
  $('#label-wave').onclick = (clickEvent) => {
    const box = $('#label-wave').getBoundingClientRect();
    const span = Math.max(data.end_offset_seconds - data.start_offset_seconds, 0.001);
    const seconds = (clickEvent.clientX - box.left - 8) / Math.max(box.width - 16, 1) * span;
    audio.currentTime = Math.max(0, Math.min(seconds, audio.duration || span));
    renderWave(audio.currentTime);
  };
}

// playSeconds: position of the audio scrubber, in seconds from the clip start
function renderWave(playSeconds = null) {
  const data = waveData;
  const canvas = $('#label-wave');
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth, height = canvas.clientHeight;
  if (!data || !width || !height) return;
  canvas.width = width * ratio; canvas.height = height * ratio;
  const ctx = canvas.getContext('2d'); ctx.scale(ratio, ratio);
  ctx.clearRect(0, 0, width, height);
  const t0 = data.start_offset_seconds, t1 = data.end_offset_seconds;
  const span = Math.max(t1 - t0, 0.001);
  // clip seconds (0 = start of the audio) map straight onto session offset t0 + s
  const x = (seconds) => 8 + (width - 16) * (seconds - t0) / span;
  const clipX = (clipSeconds) => x(t0 + clipSeconds);
  const values = data.envelope_dbfs.filter(Number.isFinite);
  const low = Math.min(...values, ...data.floor_dbfs) - 2;
  const high = Math.max(...values) + 4;
  const y = (db) => 12 + (height - 40) * (1 - (db - low) / Math.max(high - low, 1));

  // `annotated` toggles the detector's own marks. While labelling they stay off so
  // the listener judges the sound, not the detector: no suspected window, no burst
  // detections, no SpO₂. reveal() turns them on.
  const [ws, we] = data.window;
  if (waveAnnotated) {
    ctx.fillStyle = 'rgba(171,128,255,.14)';
    ctx.fillRect(x(ws), 10, Math.max(2, x(we) - x(ws)), height - 34);
    data.bursts.forEach(burst => {
      ctx.fillStyle = 'rgba(171,128,255,.55)';
      ctx.fillRect(x(burst.start), height - 26, Math.max(1.5, (width - 16) * burst.duration / span), 6);
    });
  }

  const line = (series, color, dash = []) => {
    ctx.strokeStyle = color; ctx.lineWidth = 1.3; ctx.setLineDash(dash); ctx.beginPath();
    series.forEach((value, index) => {
      const px = x(t0 + index / data.sample_rate_hz);
      const py = y(value);
      index ? ctx.lineTo(px, py) : ctx.moveTo(px, py);
    });
    ctx.stroke(); ctx.setLineDash([]);
  };
  line(data.floor_dbfs, '#4a5860');
  if (waveAnnotated) line(data.floor_dbfs.map(value => value + data.burst_threshold_db), '#6c7a84', [4, 4]);
  line(data.envelope_dbfs, '#58d6d0');

  if (waveAnnotated && data.spo2.length > 1) {
    const lows = Math.min(...data.spo2.map(p => p.value)) - 1;
    const highs = Math.max(...data.spo2.map(p => p.value)) + 1;
    ctx.strokeStyle = '#f0ad4e'; ctx.lineWidth = 1.6; ctx.beginPath();
    data.spo2.forEach((point, index) => {
      const px = x(point.offset);
      const py = 14 + (height - 46) * (1 - (point.value - lows) / Math.max(highs - lows, 1));
      index ? ctx.lineTo(px, py) : ctx.moveTo(px, py);
    });
    ctx.stroke();
    ctx.fillStyle = '#f0ad4e'; ctx.font = '10px DM Mono';
    ctx.fillText(`SpO₂ ${Math.min(...data.spo2.map(p => p.value))}–${Math.max(...data.spo2.map(p => p.value))}%`, width - 108, 14);
  }
  // second axis: a tick every 5 / 10 / 15 s depending on how long the clip is
  const stepSeconds = span > 60 ? 15 : span > 24 ? 10 : 5;
  ctx.font = '10px DM Mono'; ctx.textAlign = 'left';
  for (let sec = 0; sec <= span + 0.001; sec += stepSeconds) {
    const px = clipX(sec);
    ctx.strokeStyle = '#1b242a'; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(px, 10); ctx.lineTo(px, height - 26); ctx.stroke();
    ctx.fillStyle = '#65727b';
    ctx.fillText(`${sec}s`, Math.min(px + 3, width - 22), height - 8);
  }

  // where the window in question sits, in clip seconds
  if (waveAnnotated) {
    ctx.fillStyle = '#b79bff';
    ctx.fillText(`window ${(ws - t0).toFixed(1)}–${(we - t0).toFixed(1)}s`, Math.min(x(ws) + 4, width - 150), 20);
  }

  // playhead: follows the audio scrubber
  if (playSeconds != null && isFinite(playSeconds)) {
    const px = clipX(Math.max(0, Math.min(playSeconds, span)));
    ctx.strokeStyle = '#e9edf0'; ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.moveTo(px, 8); ctx.lineTo(px, height - 26); ctx.stroke();
    ctx.fillStyle = '#e9edf0';
    ctx.fillText(`${playSeconds.toFixed(1)}s`, Math.min(px + 4, width - 32), 32);
  }
}

async function renderStats() {
  const stats = await api(`/api/sessions/${currentSession.id}/review-stats`);
  const done = stats.candidates.labeled + stats.controls.labeled;
  const total = stats.candidates.total + stats.controls.total;
  $('#label-stats').innerHTML = [
    ['Labelled', `${done}/${total}`],
    ['Precision', stats.precision == null ? '—' : `${Math.round(stats.precision * 100)}%`],
    ['Recall (est.)', stats.recall_estimate == null ? '—' : `${Math.round(stats.recall_estimate * 100)}%`],
    ['Pauses in controls', stats.control_pause_rate == null ? '—' : `${Math.round(stats.control_pause_rate * 100)}%`],
    ['Missed events', stats.controls.pause],
    ['False positives', stats.candidates.no_pause],
  ].map(([label, value]) => `<div><b>${value}</b><span>${label}</span></div>`).join('');
}

document.querySelectorAll('[data-label]').forEach(button => {
  button.onclick = () => labelClip(button.dataset.label);
});

try {
  const saved = localStorage.getItem(GAIN_KEY);
  if (saved) $('#label-gain').value = saved;
} catch {}
$('#label-gain-val').textContent = `${Number($('#label-gain').value)}×`;
$('#label-gain').oninput = applyGain;
$('#label-compress').onchange = applyAudioChain;
$('#label-audio').addEventListener('play', () => {
  const chain = ensureAudioChain();
  if (chain) { chain.ctx.resume(); applyGain(); }
});

$('#label-next').onclick = () => { batchIndex += 1; renderClip(); };
$('#label').onclick = () => openLabeling(false);
$('#label-new').onclick = () => openLabeling(true);
$('#label-exit').onclick = () => { $('#labeling').classList.add('hidden'); $('#label-audio').removeAttribute('src'); };

$('#tz').textContent = `🕓 ${shortZone()}`;
$('#tz').onclick = () => {
  const next = (prompt('Timezone for every clip time (IANA name, e.g. Europe/Madrid, or UTC)', zone) || '').trim();
  if (!next || next === zone) return;
  try {
    new Intl.DateTimeFormat('en-GB', {timeZone: next}); // throws on an unknown zone
    zone = next;
    try { localStorage.setItem(TZ_KEY, zone); } catch {}
    announce(`Clip times now shown in ${zoneLabel()}.`);
    if (currentSession) openSession(currentSession.id); else $('#tz').textContent = `🕓 ${shortZone()}`;
  } catch { announce(`Unknown timezone: ${next}`); }
};

window.addEventListener('resize', () => { if (currentSession) openSession(currentSession.id); renderWave(); });
loadSessions().catch(error => announce(error.message));
