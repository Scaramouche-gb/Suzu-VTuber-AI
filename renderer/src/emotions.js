/**
 * Emotion manager for VRM expressions with smooth blending.
 */

export const STANDARD_EMOTIONS = [
  'neutral',
  'happy',
  'sad',
  'angry',
  'relaxed',
  'surprised',
];

export class EmotionManager {
  constructor(vrm) {
    this.vrm = vrm;
    this.currentWeights = {};
    this.targetWeights = {};
    this.holdTimers = {};
    this.blendSpeed = 6.0; // 6.0 units per second

    for (const emo of STANDARD_EMOTIONS) {
      this.currentWeights[emo] = 0.0;
      this.targetWeights[emo] = 0.0;
      this.holdTimers[emo] = 0.0;
    }
  }

  setVRM(vrm) {
    this.vrm = vrm;
  }

  setEmotion(name, intensity = 1.0, holdMs = 0) {
    const validName = STANDARD_EMOTIONS.includes(name) ? name : 'neutral';
    const clampedIntensity = Math.max(0, Math.min(1, intensity));

    // Reset others if setting a primary emotion (unless neutral)
    for (const emo of STANDARD_EMOTIONS) {
      if (emo === validName) {
        this.targetWeights[emo] = clampedIntensity;
        this.holdTimers[emo] = holdMs > 0 ? holdMs / 1000.0 : 0.0;
      } else {
        this.targetWeights[emo] = 0.0;
        this.holdTimers[emo] = 0.0;
      }
    }
  }

  update(delta) {
    if (!this.vrm || !this.vrm.expressionManager) return;

    for (const emo of STANDARD_EMOTIONS) {
      // Check hold timers
      if (this.holdTimers[emo] > 0) {
        this.holdTimers[emo] -= delta;
        if (this.holdTimers[emo] <= 0) {
          this.targetWeights[emo] = 0.0;
        }
      }

      // Smooth exponential interpolation
      const current = this.currentWeights[emo];
      const target = this.targetWeights[emo];
      const next = current + (target - current) * Math.min(1.0, delta * this.blendSpeed);
      this.currentWeights[emo] = next;

      // Apply to VRM expression manager if available
      try {
        this.vrm.expressionManager.setValue(emo, next);
      } catch {
        // Fallback for VRM without this specific expression
      }
    }
  }
}
