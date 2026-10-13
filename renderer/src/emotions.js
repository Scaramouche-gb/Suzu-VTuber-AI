/**
 * Emotion manager supporting standard VRM 1.0 presets and rich custom VRM 0.0 blend shapes.
 */

export const EMOTION_MAP = {
  // Joy / Happy
  happy: ['happy02', 'happy01', 'happy', 'joy'],
  cheerful: ['happy02', 'happy01', 'happy', 'joy'],
  smile: ['happy01', 'happy02', 'happy', 'joy'],
  laugh: ['happy08', 'happy02', 'happy'],
  excited: ['happy08', 'happy03', 'happy'],

  // Playful / Smug / Derp
  playful: ['happy04', 'happy05', 'silly', 'happy'],
  smug: ['happy04', 'happy05', 'happy'],
  silly: ['silly', 'happy04', 'happy'],
  teasing: ['happy04', 'happy05', 'silly'],

  // Questioning / Thinking
  curious: ['doubt', 'happy01', 'relaxed'],
  thinking: ['doubt', 'neutral'],
  doubt: ['doubt', 'neutral'],
  skeptical: ['doubt', 'neutral'],

  // Surprise
  surprised: ['happy03', 'doubt', 'surprised'],
  shocked: ['happy03', 'surprised'],

  // Sad / Worried / Flustered
  sad: ['sad01', 'sad02', 'sad', 'sorrow'],
  worried: ['sad02', 'sweat', 'sad'],
  sympathetic: ['sad04', 'happy01', 'relaxed'],
  embarrassed: ['sweat', 'happy07', 'happy01'],
  awkward: ['sweat', 'happy01'],
  sweat: ['sweat', 'happy01'],
  shy: ['happy07', 'sweat', 'happy01'],

  // Anger / Pouting
  angry: ['angry01', 'angry02', 'angry'],
  annoyed: ['angry03', 'angry01', 'angry'],
  pout: ['angry03', 'angry01'],
  sassy: ['happy04', 'angry03'],

  // Relaxed / Neutral
  relaxed: ['happy01', 'relaxed', 'fun'],
  sleepy: ['happy01', 'relaxed'],
  neutral: [],
};

export const STANDARD_EMOTIONS = Object.keys(EMOTION_MAP);

export const ALL_MORPH_NAMES = [
  'happy01', 'happy02', 'happy03', 'happy04', 'happy05', 'happy06', 'happy07', 'happy08',
  'angry01', 'angry02', 'angry03', 'angry04',
  'sad01', 'sad02', 'sad03', 'sad04', 'sad05',
  'silly', 'sweat', 'doubt',
  'happy', 'sad', 'angry', 'relaxed', 'surprised',
];

export class EmotionManager {
  constructor(vrm = null) {
    this.vrm = vrm;
    this.currentWeights = new Map();
    this.targetWeights = new Map();
    this.holdTimer = 0.0;
    this.currentEmotion = 'neutral';
    this.blendSpeed = 7.0; // Fast and expressive transitions

    for (const name of ALL_MORPH_NAMES) {
      this.currentWeights.set(name, 0.0);
      this.targetWeights.set(name, 0.0);
    }
  }

  setVRM(vrm) {
    this.vrm = vrm;
  }

  setEmotion(name, intensity = 1.0, holdMs = 0) {
    const rawKey = (name || 'neutral').toLowerCase().trim();
    this.currentEmotion = rawKey;
    const clampedIntensity = Math.max(0.0, Math.min(1.0, intensity));
    this.holdTimer = holdMs > 0 ? holdMs / 1000.0 : 0.0;

    // Zero out all target weights first
    for (const key of this.targetWeights.keys()) {
      this.targetWeights.set(key, 0.0);
    }

    if (rawKey === 'neutral') {
      return;
    }

    // Resolve candidates from map or directly
    const candidates = EMOTION_MAP[rawKey] || [rawKey];
    let resolved = false;

    // Apply primary matched target
    for (const candidate of candidates) {
      if (this.vrm && this.vrm.expressionManager) {
        // Test if expression exists on the model
        const expr = this.vrm.expressionManager.getExpression(candidate);
        if (expr) {
          this.targetWeights.set(candidate, clampedIntensity);
          resolved = true;
          break;
        }
      } else {
        this.targetWeights.set(candidate, clampedIntensity);
        resolved = true;
        break;
      }
    }

    // Fallback: if specific candidate wasn't found in expressionManager, try first candidate
    if (!resolved && candidates.length > 0) {
      this.targetWeights.set(candidates[0], clampedIntensity);
    }
  }

  update(delta) {
    if (!this.vrm || !this.vrm.expressionManager) return;

    if (this.holdTimer > 0) {
      this.holdTimer -= delta;
      if (this.holdTimer <= 0) {
        this.setEmotion('neutral', 1.0, 0);
      }
    }

    for (const [name, target] of this.targetWeights.entries()) {
      const current = this.currentWeights.get(name) || 0.0;
      if (Math.abs(current - target) > 0.001 || target > 0.0) {
        const next = current + (target - current) * Math.min(1.0, delta * this.blendSpeed);
        this.currentWeights.set(name, next);

        try {
          this.vrm.expressionManager.setValue(name, next);
        } catch {
          // Ignore unsupported morph
        }
      }
    }
  }
}
