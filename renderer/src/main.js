import { AvatarController } from './avatar.js';
import { EmotionManager, STANDARD_EMOTIONS } from './emotions.js';
import { LipsyncManager } from './lipsync.js';
import { GestureManager } from './gestures.js';
import { VTuberWSClient } from './ws.js';

// Parse query params
const params = new URLSearchParams(window.location.search);
const fitMode = params.get('fit') || 'bust';
const scale = parseFloat(params.get('scale') || '1.0');
const yOffset = parseFloat(params.get('y') || '0.0');
const modelUrl = params.get('model') || '/models/avatar.vrm';
const targetFps = parseInt(params.get('fps') || '30', 10);

const canvas = document.getElementById('avatar-canvas');
const avatar = new AvatarController(canvas);
avatar.setFit(fitMode, scale, yOffset);

const emotions = new EmotionManager();
const lipsync = new LipsyncManager();
// By default daemon plays audio directly via PipeWire mpv; lipsync only calculates mouth movement
const desktopAudio = params.get('desktop_audio') !== '0';
lipsync.muteOutput = desktopAudio;

const gestures = new GestureManager();
const ws = new VTuberWSClient();

// Wire up lipsync callbacks with WS
lipsync.onPlaybackStarted = (id, item) => {
  avatar.setState('speaking');
  if (item && item.emotion) {
    emotions.setEmotion(item.emotion, 0.9, 0);
  }
  if (item && item.gesture) {
    gestures.playGesture(item.gesture);
  }
  ws.sendPlaybackStarted(id);
};
lipsync.onPlaybackEnded = (id, item) => {
  if (lipsync.queue.length === 0) {
    avatar.setState('idle');
    emotions.setEmotion('neutral', 1.0, 0);
  }
  ws.sendPlaybackEnded(id);
};

// Unlock Web Audio context on any user interaction
const unlockAudio = () => {
  lipsync.ensureContext();
};
window.addEventListener('pointerdown', unlockAudio, { passive: true });
window.addEventListener('keydown', (e) => {
  unlockAudio();
  if (e.key === 'f' || e.key === 'F') {
    avatar.flip();
  }
});
window.addEventListener('dblclick', () => {
  avatar.flip();
});

// Handle clicks on avatar
canvas.addEventListener('click', (e) => {
  unlockAudio();
  const rect = canvas.getBoundingClientRect();
  const yRatio = (e.clientY - rect.top) / rect.height;
  const region = yRatio < 0.4 ? 'head' : 'body';
  ws.sendClick(region);

  // Play micro visual reaction locally
  if (region === 'head') {
    emotions.setEmotion('surprised', 0.8, 1500);
    gestures.playGesture('nod');
  } else {
    gestures.playGesture('shrug');
  }
});

// Handle WebSocket messages
ws.on('state', (data) => {
  console.log('[Daemon State]', data.value);
  avatar.setState(data.value);
  if (data.value === 'thinking') {
    avatar.setLookTarget('point', 0.8, 0.1);
  } else if (data.value === 'listening') {
    avatar.setLookTarget('user');
  } else if (data.value === 'idle') {
    emotions.setEmotion('neutral', 1.0, 0);
  }
});

ws.on('record_round', async (data) => {
  unlockAudio();
  const recordId = data.id;
  const audioUrl = data.audio_url;
  const emotion = data.emotion || 'playful';
  const gesture = data.gesture || 'wave';

  // 1. Preload audio buffer first so video recording starts instantly without network latency
  let audioBuffer;
  try {
    audioBuffer = await lipsync.preloadAudio(audioUrl);
  } catch (err) {
    console.error('Failed to preload round audio:', err);
    return;
  }

  // 2. Adjust camera for selfie portrait "кружок" framing
  avatar.setFit('bust', 1.35, 0.08);
  avatar.setLookTarget('user');
  emotions.setEmotion(emotion, 0.9, 0);
  if (gesture) gestures.playGesture(gesture);

  let stream;
  try {
    stream = canvas.captureStream(30);
  } catch (err) {
    console.error('Failed to capture canvas stream:', err);
    return;
  }

  let mediaRecorder;
  try {
    mediaRecorder = new MediaRecorder(stream, {
      mimeType: MediaRecorder.isTypeSupported('video/webm;codecs=vp8')
        ? 'video/webm;codecs=vp8'
        : 'video/webm',
      videoBitsPerSecond: 2500000,
    });
  } catch (err) {
    console.error('Failed to init MediaRecorder:', err);
    return;
  }

  const chunks = [];
  mediaRecorder.ondataavailable = (e) => {
    if (e.data && e.data.size > 0) chunks.push(e.data);
  };

  mediaRecorder.onstop = async () => {
    const blob = new Blob(chunks, { type: 'video/webm' });
    avatar.setFit(fitMode, scale, yOffset); // Restore standard overlay camera framing
    emotions.setEmotion('neutral', 1.0, 0);

    try {
      await fetch(`/api/record_round_done?id=${encodeURIComponent(recordId)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'video/webm' },
        body: blob,
      });
      console.log('[Recorder] Successfully uploaded round video blob for', recordId);
    } catch (err) {
      console.error('[Recorder] Upload failed:', err);
    }
  };

  // 3. Start video stream recording and audio buffer playback synchronously in the exact same millisecond
  mediaRecorder.start();
  lipsync.playDecodedBuffer(audioBuffer, `round_${recordId}`, () => {
    setTimeout(() => {
      if (mediaRecorder.state !== 'inactive') {
        mediaRecorder.stop();
      }
    }, 150); // 150ms grace to capture mouth closing
  });
});

ws.on('say', (data) => {
  unlockAudio();
  if (data.audio_url) {
    lipsync.enqueue({
      id: data.id,
      audioUrl: data.audio_url,
      emotion: data.emotion,
      gesture: data.gesture,
    });
  } else {
    if (data.emotion) {
      emotions.setEmotion(data.emotion, data.intensity || 0.8, 0);
    }
    if (data.gesture) {
      gestures.playGesture(data.gesture);
    }
  }
});

ws.on('emotion', (data) => {
  emotions.setEmotion(data.name, data.intensity || 1.0, data.hold_ms || 0);
});

ws.on('gesture', (data) => {
  gestures.playGesture(data.name);
});

ws.on('look', (data) => {
  avatar.setLookTarget(data.target, data.x, data.y);
});

ws.on('stop', () => {
  lipsync.stop();
  emotions.setEmotion('neutral', 1.0, 0);
});

ws.on('config', (data) => {
  if (data.scale !== undefined || data.y_offset !== undefined) {
    avatar.setFit(fitMode, data.scale ?? scale, data.y_offset ?? yOffset);
  }
});

// Load model
async function init() {
  const statusEl = document.getElementById('load-status');
  if (statusEl) statusEl.textContent = 'Загрузка VRM...';

  try {
    const vrm = await avatar.loadModel(modelUrl);
    emotions.setVRM(vrm);
    lipsync.setVRM(vrm);
    gestures.setVRM(vrm, avatar.mixer);

    if (statusEl) statusEl.style.display = 'none';
    console.log('[VRM] Model loaded successfully');
  } catch (err) {
    console.error('[VRM] Failed to load model:', err);
    if (statusEl) statusEl.textContent = 'Ошибка загрузки: ' + err.message;
  }
}

init();

// Animation loop with FPS throttle
let lastTime = performance.now();
const interval = 1000 / targetFps;

function animate(currentTime) {
  requestAnimationFrame(animate);

  const deltaMs = currentTime - lastTime;
  if (deltaMs < interval) return;

  const delta = Math.min(deltaMs / 1000, 0.1);
  lastTime = currentTime - (deltaMs % interval);

  // 1. Prepare base pose with natural idle, breathing, and hip/spine sway
  avatar.setSpeechVolume(lipsync.currentVolume);
  avatar.preparePose(delta);

  // 2. Layer procedural gestures (nod, shrug, wave, think, laugh) on top
  gestures.update(delta);

  // 3. Apply facial expressions and lipsync morph targets
  emotions.update(delta);
  lipsync.update(delta);

  // 4. Update spring physics (hair, clothing, ribbons) and render frame
  avatar.finishFrame(delta);
}

requestAnimationFrame(animate);

// Export for debug panel
window.VTUBER = {
  avatar,
  emotions,
  lipsync,
  gestures,
  ws,
  STANDARD_EMOTIONS,
};
