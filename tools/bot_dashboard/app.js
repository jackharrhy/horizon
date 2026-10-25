const $ = id => document.getElementById(id);
let latest, actionRevision = 0, streamId = null, abortStream = null, mediaUrl = null, refreshing = false, streamBusy = false;
let wantsStream = false;
let playback = null, frameCallback = null;

function measurePlayback(now, metadata) {
  if (!playback) return;
  playback.presented = metadata.presentedFrames;
  if (playback.lastPresented == null) { playback.lastPresented = metadata.presentedFrames; playback.measuredAt = now; }
  if (now - playback.measuredAt >= 1500) {
    playback.fps = (metadata.presentedFrames - playback.lastPresented) * 1000 / (now - playback.measuredAt);
    playback.lastPresented = metadata.presentedFrames; playback.measuredAt = now;
  }
  frameCallback = $('video').requestVideoFrameCallback(measurePlayback);
}

$('video').addEventListener('waiting', () => { if (playback?.presented > 0 && !$('video').paused) playback.stalls++; });

function notice(message, error = false) {
  $('notice').textContent = message;
  $('notice').classList.toggle('error', error);
  $('notice').hidden = !message;
}

async function request(action, extra = {}) {
  const response = await fetch('/api/action', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({action,...extra})});
  const data = await response.json();
  if (!response.ok || data.error) throw new Error(data.error || 'The action could not be completed.');
  return data;
}

async function act(action, extra = {}) {
  const revision = ++actionRevision;
  notice('');
  try {
    const data = await request(action, extra);
    if (revision === actionRevision && action === 'goto') notice('Arrived.');
    refresh();
    return data;
  } catch (error) {
    if (revision === actionRevision) notice(error.message, true);
  }
}

function keepFrame() {
  const video = $('video');
  if (video.videoWidth && video.readyState >= 2) {
    const canvas = document.createElement('canvas');
    canvas.width = video.videoWidth; canvas.height = video.videoHeight;
    canvas.getContext('2d').drawImage(video, 0, 0);
    $('snapshot').src = canvas.toDataURL('image/jpeg', .85);
    $('snapshot').hidden = false;
    $('empty-view').hidden = true;
    $('view-label').textContent = 'Last frame'; $('view-label').hidden = false;
  }
}

function detachStream() {
  keepFrame();
  abortStream?.abort(); abortStream = null;
  if (frameCallback != null) $('video').cancelVideoFrameCallback(frameCallback);
  frameCallback = null; playback = null; $('playback-detail').hidden = true;
  $('video').playbackRate = 1;
  $('video').pause(); $('video').removeAttribute('src'); $('video').load(); $('video').hidden = true;
  if (mediaUrl) URL.revokeObjectURL(mediaUrl);
  mediaUrl = null; streamId = null;
}

async function append(buffer, operation) {
  await new Promise((resolve, reject) => {
    const done = () => { cleanup(); resolve(); };
    const failed = () => { cleanup(); reject(new Error('The browser could not decode the stream.')); };
    const cleanup = () => { buffer.removeEventListener('updateend', done); buffer.removeEventListener('error', failed); };
    buffer.addEventListener('updateend', done); buffer.addEventListener('error', failed);
    try { operation(); } catch (error) { cleanup(); reject(error); }
  });
}

async function attachStream(stream) {
  if (!stream.ready || streamId === stream.id) return;
  detachStream();
  if (!window.MediaSource || !MediaSource.isTypeSupported(stream.mime)) throw new Error('This browser cannot play H.264/AAC live video. Try Chrome or Safari.');
  streamId = stream.id;
  const controller = new AbortController(); abortStream = controller;
  try {
  const source = new MediaSource(); mediaUrl = URL.createObjectURL(source);
  const video = $('video'); video.src = mediaUrl; video.muted = !$('audio').checked; video.hidden = false;
  playback = {started:false, fps:null, stalls:0, recoveries:0, presented:0};
  if (video.requestVideoFrameCallback) frameCallback = video.requestVideoFrameCallback(measurePlayback);
  $('snapshot').hidden = true; $('empty-view').hidden = true; $('view-label').textContent = 'Live'; $('view-label').hidden = false;
  $('stream-state').textContent = 'Connecting';
  await new Promise(resolve => {
    const ready = () => {controller.signal.removeEventListener('abort', ready);source.removeEventListener('sourceopen', ready);resolve();};
    source.addEventListener('sourceopen', ready, {once:true});
    controller.signal.addEventListener('abort', ready, {once:true});
  });
  if (controller.signal.aborted) return;
  const buffer = source.addSourceBuffer(stream.mime);
  const response = await fetch('/stream/' + stream.id, {signal:controller.signal});
  if (!response.ok) throw new Error('The stream stopped before the player connected.');
  const reader = response.body.getReader();
    while (!controller.signal.aborted) {
      const {value, done} = await reader.read();
      if (done) throw new Error('Stream connection ended; reconnecting.');
      await append(buffer, () => buffer.appendBuffer(value));
      if (buffer.buffered.length) {
        const start = buffer.buffered.start(0), end = buffer.buffered.end(buffer.buffered.length - 1);
        const headroom = end - video.currentTime;
        if (!playback.started && end - start >= .6) {
          video.currentTime = Math.max(start, end - .6);
          playback.started = true;
          video.play().catch(error => {
            if (controller.signal.aborted || error.name === 'AbortError') return;
            if (error.name === 'NotAllowedError' && !video.muted) {
              video.muted = true;
              $('audio').checked = false;
              notice('Click Sound to unmute.');
              request('stream-start', {audio:false, fps:Number($('fps').value)}).catch(error => notice(error.message, true));
              video.play().catch(() => notice('Press Play to watch.'));
            } else {
              notice(error.message, true);
            }
          });
        } else if (playback.started && !video.paused) {
          // Normal drift is corrected gently, never by repeatedly flushing the decoder with seeks.
          if (video.currentTime < start - .05 || headroom > 2.5) {
            video.currentTime = Math.max(start, end - .6);
            playback.recoveries++;
          }
          video.playbackRate = headroom > .85 ? 1.03 : headroom < .3 ? .97 : 1;
        }
        if (source.setLiveSeekableRange) source.setLiveSeekableRange(start, end);
        if (end - start > 8) {
          const cutoff = end - 6;
          if (video.currentTime < cutoff) video.currentTime = end - .6;
          await append(buffer, () => buffer.remove(start, cutoff));
        }
      }
    }
    if (source.readyState === 'open' && !buffer.updating) source.endOfStream();
  } catch (error) {
    if (!controller.signal.aborted && abortStream === controller) {
      detachStream();
      if (!streamBusy) notice(error.message, true);
    }
  }
}

async function configureStream() {
  if (streamBusy) return;
  streamBusy = true; $('stream-toggle').disabled = true;
  $('audio').disabled = true; $('fps').disabled = true;
  $('stream-toggle').textContent = wantsStream ? 'Starting...' : 'Stopping...';
  notice('');
  try {
    if (wantsStream) {
      const stream = await request('stream-start', {audio:$('audio').checked, fps:Number($('fps').value)});
      attachStream(stream).catch(error => notice(error.message, true));
    } else {
      detachStream(); await request('stream-stop');
    }
  } catch (error) {
    wantsStream = false; detachStream(); notice(error.message, true);
  } finally {
    streamBusy = false; $('stream-toggle').disabled = false;
    $('audio').disabled = false; $('fps').disabled = false;
    $('stream-toggle').textContent = wantsStream ? 'Stop video' : 'Start video';
    refresh();
  }
}

async function capture() {
  $('capture').disabled = true;
  try {
    const result = await request('snapshot');
    if (!wantsStream) {
      $('snapshot').src = '/snapshot.png?t=' + Date.now(); $('snapshot').hidden = false;
      $('empty-view').hidden = true; $('view-label').hidden = false;
      $('view-label').textContent = 'Snapshot';
    }
    $('capture-note').hidden = false;
    $('capture-note').textContent = `Snapshot: tick ${result.capture.tick.toLocaleString()}, ${result.width} × ${result.height}, ${result.capture.pending_chunks} chunks pending.`;
    refresh();
  } catch (error) { notice(error.message, true); }
  finally { $('capture').disabled = false; }
}

async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    const response = await fetch('/api/state'); const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'The bot is not responding.');
    latest = data;
    $('connection').textContent = data.connected ? 'Connected' : 'Disconnected';
    $('connection').classList.toggle('error', !data.connected);
    $('address').textContent = data.server;
    const state = data.state, player = state.player;
    $('health').textContent = player ? `${Math.ceil(player.health)} / 20` : '...';
    $('food').textContent = player ? `${player.food} / 20` : '...';
    $('position').textContent = player ? player.position.map(v => v.toFixed(1)).join(' / ') : 'Not spawned';
    $('tick-rate').textContent = data.tick_rate == null ? '...' : `${data.tick_rate.toFixed(1)} Hz`;
    $('behaviour').textContent = state.screen === 'dead' ? 'Dead' : state.following ? 'Following' : state.busy ? 'Busy' : 'Idle';
    const names = data.players.filter(name => name !== 'HorizonLabBot');
    const selected = $('player').value || 'cheesemany';
    if (JSON.stringify(names) !== $('player').dataset.names) {
      $('player').replaceChildren(...(names.length ? names.map(name => new Option(name,name)) : [new Option('No players online','')]));
      $('player').dataset.names = JSON.stringify(names);
      if (names.includes(selected)) $('player').value = selected;
    }
    $('follow').disabled = !names.length || !data.connected;
    $('respawn').disabled = state.screen !== 'dead';
    $('respawn').hidden = state.screen !== 'dead';
    const stream = data.stream;
    if (wantsStream && stream.active && stream.id !== streamId && !streamBusy) attachStream(stream).catch(error => notice(error.message, true));
    if (wantsStream && !stream.active && !streamBusy) {
      wantsStream = false; detachStream(); $('stream-toggle').textContent = 'Start video';
      if (stream.error) notice(stream.error, true);
    }
    $('stream-state').textContent = stream.active ? (stream.ready ? 'Live' : 'Starting') : 'Off';
    $('stream-detail').textContent = stream.active ? `${stream.size[0]} × ${stream.size[1]} · ${stream.capture_fps} fps captured · H.264${stream.audio ? ' / AAC' : ''}` : 'Video off';
    if (playback) {
      const video = $('video'), quality = video.getVideoPlaybackQuality?.();
      const headroom = video.buffered.length ? Math.max(0, video.buffered.end(video.buffered.length-1) - video.currentTime) : 0;
      $('playback-detail').hidden = false;
      $('playback-detail').textContent = `${playback.fps == null ? 'Measuring playback' : playback.fps.toFixed(0)+' fps playback'} · ${stream.encoder === 'h264_videotoolbox' ? 'Apple hardware H.264' : 'Software H.264'} · ${(headroom*1000).toFixed(0)} ms buffered · ${quality?.droppedVideoFrames || 0} dropped · ${playback.stalls} stalls`;
    }
    $('activity').replaceChildren(...(data.activity.length ? data.activity.slice(0,5).map(item => {
      const li=document.createElement('li'), time=document.createElement('time'); time.textContent=item.time;
      li.append(time,document.createTextNode(item.message)); li.classList.toggle('error',item.error); return li;
    }) : [Object.assign(document.createElement('li'), {className:'subtle',textContent:'Nothing yet.'})]));
    $('runtime-note').hidden = !data.decode_errors;
    $('runtime-note').textContent = `${data.decode_errors} packet decode errors`;
    if ($('diagnostics').open) $('raw-state').textContent = JSON.stringify(data,null,2);
  } catch (error) {
    $('connection').textContent = 'Unavailable'; $('connection').classList.add('error');
    notice(error.message, true);
  } finally { refreshing=false; }
}

$('follow-form').addEventListener('submit', event => {event.preventDefault(); act('follow',{name:$('player').value});});
$('goto-form').addEventListener('submit', event => {event.preventDefault(); act('goto',{x:Number($('x').value),y:Number($('y').value),z:Number($('z').value)});});
for (const action of ['stop','jump','respawn','reset']) $(action).addEventListener('click',()=>act(action));
$('capture').addEventListener('click',capture);
$('stream-toggle').addEventListener('click',()=>{wantsStream=!wantsStream;configureStream();});
$('audio').addEventListener('change', () => {
  const video = $('video');
  // Unmute during the click, not after the network request loses user activation.
  video.muted = !$('audio').checked;
  if (wantsStream) {
    if (video.paused && playback?.started) video.play().catch(() => notice('Press Play to watch.'));
    configureStream();
  }
});
$('fps').addEventListener('change',()=>{if(wantsStream)configureStream();});
$('address').addEventListener('click',()=>navigator.clipboard.writeText(latest?.server || '').then(()=>notice('Copied.')).catch(()=>notice('Server: '+latest?.server)));
$('diagnostics').addEventListener('toggle',()=>{if(latest)$('raw-state').textContent=JSON.stringify(latest,null,2);});
window.addEventListener('pagehide',()=>{abortStream?.abort();});
setInterval(refresh,700);refresh();
