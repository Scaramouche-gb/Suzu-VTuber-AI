/**
 * Lifelike Motion-Capture & VRMA Gesture Engine for Suzu VTuber.
 * Loads and blends real human motion capture (.bvh) and official Pixiv VRM animations (.vrma)
 * with smooth cubic cross-fading, idle looping, and seamless fallback.
 */

import * as THREE from 'three';
import { AnimationLoader } from './animation-loader.js';

export const GESTURE_MAP = {
  // --- Idles & Stances ---
  'idle': '/anims/neutral_idle.bvh',
  'idle2': '/anims/neutral_idle2.bvh',
  'neutral': '/anims/neutral.bvh',
  'caring': '/anims/caring.bvh',

  // --- Greetings & Waves ---
  'wave': '/anims/greeting.vrma',
  'greeting': '/anims/greeting.vrma',
  'greeting2': '/anims/greeting2.vrma',
  'hello': '/anims/hello.vrma',
  'hi': '/anims/hello.vrma',

  // --- Affirmation & Nodding ---
  'nod': '/anims/approval.bvh',
  'approval': '/anims/approval.bvh',
  'yes': '/anims/approval2.bvh',
  'agree': '/anims/approval3.bvh',

  // --- Disagreement & Shaking Head ---
  'shake': '/anims/disapproval.bvh',
  'disapproval': '/anims/disapproval.bvh',
  'no': '/anims/disapproval.bvh',
  'disagree': '/anims/disapproval.bvh',

  // --- Thoughtful & Curious ---
  'think': '/anims/curiosity.bvh',
  'thinking': '/anims/curiosity.bvh',
  'curious': '/anims/curiosity2.bvh',
  'curiosity': '/anims/curiosity3.bvh',

  // --- Confusion & Shrugging ---
  'shrug': '/anims/confusion.bvh',
  'confusion': '/anims/confusion.bvh',
  'confused': '/anims/confusion2.bvh',

  // --- Amusement & Laughter ---
  'laugh': '/anims/amusement.bvh',
  'amusement': '/anims/amusement.bvh',
  'giggle': '/anims/amusement2.bvh',
  'chuckle': '/anims/amusement3.bvh',

  // --- Disappointment, Sorrow & Facepalm ---
  'facepalm': '/anims/disappointment.bvh',
  'disappointment': '/anims/disappointment.bvh',
  'sad': '/anims/sadness.bvh',
  'sadness': '/anims/sadness.bvh',
  'grief': '/anims/grief.bvh',
  'remorse': '/anims/remorse.bvh',

  // --- Joy & Excitement ---
  'joy': '/anims/joy.bvh',
  'happy': '/anims/joy.bvh',
  'excited': '/anims/excitement.bvh',
  'excitement': '/anims/excitement.bvh',
  'bounce': '/anims/excitement2.bvh',
  'optimism': '/anims/optimism.bvh',

  // --- Surprise & Shock ---
  'surprise': '/anims/surprise.bvh',
  'shock': '/anims/surprise.bvh',
  'surprised': '/anims/surprise2.bvh',

  // --- Pride & Smugness ---
  'smug': '/anims/pride.bvh',
  'pride': '/anims/pride.bvh',

  // --- Cool & Anime Stylized VRMA Animations ---
  'peace': '/anims/peace_sign.vrma',
  'peace_sign': '/anims/peace_sign.vrma',
  'shoot': '/anims/shoot.vrma',
  'spin': '/anims/spin.vrma',
  'pose': '/anims/model_pose.vrma',
  'model_pose': '/anims/model_pose.vrma',
  'motion_pose': '/anims/motion_pose.vrma',
  'show_full_body': '/anims/show_full_body.vrma',

  // --- Relief & Emotional Nuances ---
  'relief': '/anims/relief.bvh',
  'nervous': '/anims/nervousness.bvh',
  'embarrassment': '/anims/embarrassment.bvh',
  'admiration': '/anims/admiration.bvh',
  'gratitude': '/anims/gratitude.bvh',
  'love': '/anims/love.bvh',

  // --- Dancing & Energetic Actions ---
  'dance': '/anims/dance_1.bvh',
  'dance1': '/anims/dance_1.bvh',
  'dance2': '/anims/dance_2.bvh',
  'attention': '/anims/action_attention_seeking.bvh',
};

export class GestureManager {
  constructor(vrm = null, mixer = null) {
    this.vrm = vrm;
    this.mixer = mixer;
    this.animLoader = null;
    this.avatar = null;

    this.idleAction = null;
    this.currentAction = null;
    this.isActionPlaying = false;
    this.activeGestureName = null;
    this._restoreIdleTimer = null;

    // Procedural fallback
    this.proceduralGesture = null;
    this.proceduralTimer = 0.0;
    this.proceduralDuration = 1.0;
    this.proceduralBlend = 0.0;
  }

  setAvatar(avatar) {
    this.avatar = avatar;
  }

  async setVRM(vrm, mixer) {
    this.vrm = vrm;
    this.mixer = mixer;

    if (!this.vrm || !this.mixer) return;

    this.animLoader = new AnimationLoader(vrm);

    // Initialize natural mocap idle
    await this.initIdle();

    // Preload top conversational gestures in the background
    this.preloadCommonGestures();
  }

  async initIdle() {
    try {
      const idleClip = await this.animLoader.loadAnimation(GESTURE_MAP['idle']);
      if (idleClip && this.mixer) {
        this.idleAction = this.mixer.clipAction(idleClip);
        this.idleAction.setLoop(THREE.LoopRepeat);
        this.idleAction.fadeIn(0.5).play();

        if (this.avatar && typeof this.avatar.setMocapIdleActive === 'function') {
          this.avatar.setMocapIdleActive(true);
        }
        console.log('[GestureManager] Lifelike Mocap Idle activated');
      }
    } catch (err) {
      console.warn('[GestureManager] Could not initialize mocap idle, using procedural:', err);
    }
  }

  preloadCommonGestures() {
    const common = ['wave', 'nod', 'shake', 'think', 'shrug', 'laugh', 'peace', 'joy'];
    for (const key of common) {
      const url = GESTURE_MAP[key];
      if (url) {
        this.animLoader.loadAnimation(url).catch(() => {});
      }
    }
  }

  async playGesture(name) {
    if (!this.vrm || !this.mixer) return;

    const cleanName = (name || '').toLowerCase().trim();
    if (!cleanName) return;

    const animUrl = GESTURE_MAP[cleanName];

    if (animUrl && this.animLoader) {
      try {
        const clip = await this.animLoader.loadAnimation(animUrl);
        if (clip && this.mixer) {
          const action = this.mixer.clipAction(clip);
          action.reset();
          action.setLoop(THREE.LoopOnce);
          action.clampWhenFinished = true;

          const prev = this.currentAction || this.idleAction;
          if (prev && prev !== action) {
            action.crossFadeFrom(prev, 0.35, true);
          } else {
            action.fadeIn(0.35);
          }

          action.play();
          this.currentAction = action;
          this.isActionPlaying = true;
          this.activeGestureName = cleanName;

          if (this._restoreIdleTimer) {
            clearTimeout(this._restoreIdleTimer);
          }

          // Return to idle just before the clip concludes
          const durationSec = clip.duration;
          const returnDelayMs = Math.max(800, (durationSec - 0.40) * 1000);
          this._restoreIdleTimer = setTimeout(() => {
            this.returnToIdle();
          }, returnDelayMs);

          console.log(`[GestureManager] Playing mocap gesture '${cleanName}' (${durationSec.toFixed(2)}s)`);
          return;
        }
      } catch (err) {
        console.warn(`[GestureManager] Error playing mocap gesture '${cleanName}':`, err);
      }
    }

    // Graceful procedural fallback
    this.playProcedural(cleanName);
  }

  returnToIdle() {
    if (this.currentAction && this.idleAction && this.currentAction !== this.idleAction) {
      this.idleAction.reset();
      this.idleAction.crossFadeFrom(this.currentAction, 0.45, true);
      this.idleAction.play();
    }
    this.currentAction = null;
    this.isActionPlaying = false;
    this.activeGestureName = null;
  }

  playProcedural(name) {
    this.proceduralGesture = name;
    this.proceduralTimer = 0.0;
    this.proceduralDuration = 1.4;
  }

  update(delta) {
    // If procedural fallback is active, update envelope
    if (!this.proceduralGesture || !this.vrm || !this.vrm.humanoid) {
      this.proceduralBlend = Math.max(0.0, this.proceduralBlend - delta * 4.0);
      return;
    }

    this.proceduralTimer += delta;
    const progress = Math.min(1.0, this.proceduralTimer / this.proceduralDuration);

    let envelope = 0.0;
    if (progress < 0.25) {
      const t = progress / 0.25;
      envelope = t * t * (3 - 2 * t);
    } else if (progress < 0.75) {
      envelope = 1.0;
    } else {
      const t = (1.0 - progress) / 0.25;
      envelope = t * t * (3 - 2 * t);
    }
    this.proceduralBlend = envelope;

    const humanoid = this.vrm.humanoid;
    const head = humanoid.getNormalizedBoneNode('head');
    const neck = humanoid.getNormalizedBoneNode('neck');

    if (this.proceduralGesture === 'nod' && head) {
      const nod = Math.sin(progress * Math.PI * 4) * 0.10 * envelope;
      head.rotation.x += nod;
      if (neck) neck.rotation.x += nod * 0.5;
    } else if (this.proceduralGesture === 'shake' && head) {
      head.rotation.y += Math.sin(progress * Math.PI * 5) * 0.12 * envelope;
    }

    if (progress >= 1.0) {
      this.proceduralGesture = null;
    }
  }
}
