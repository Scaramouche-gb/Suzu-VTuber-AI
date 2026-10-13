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
const gestures = new GestureManager();
const ws = new VTuberWSClient();

// Wire up lipsync callbacks with WS
lipsync.onPlaybackStarted = (id) => {
  ws.sendPlaybackStarted(id);
};
lipsync.onPlaybackEnded = (id) => {
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
  if (data.value === 'thinking') {
    avatar.setLookTarget('point', 0.8, 0.1);
  } else if (data.value === 'listening') {
    avatar.setLookTarget('user');
  }
});

ws.on('say', (data) => {
  unlockAudio();
  if (data.emotion) {
    emotions.setEmotion(data.emotion, data.intensity || 0.8, 0);
  }
  if (data.gesture) {
    gestures.playGesture(data.gesture);
  }
  if (data.audio_url) {
    lipsync.enqueue({
      id: data.id,
      audioUrl: data.audio_url,
      emotion: data.emotion,
      gesture: data.gesture,
    });
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

  emotions.update(delta);
  lipsync.update(delta);
  gestures.update(delta);
  avatar.update(delta);
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
