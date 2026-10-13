/**
 * Gesture controller supporting procedural animations and VRMA clip playback.
 */

export class GestureManager {
  constructor(vrm, mixer) {
    this.vrm = vrm;
    this.mixer = mixer;
    this.activeGesture = null;
    this.gestureTimer = 0;
    this.gestureDuration = 0;
  }

  setVRM(vrm, mixer) {
    this.vrm = vrm;
    this.mixer = mixer;
  }

  playGesture(name) {
    if (!this.vrm) return;
    this.activeGesture = name;
    this.gestureTimer = 0;

    switch (name) {
      case 'nod':
        this.gestureDuration = 1.2;
        break;
      case 'shake':
        this.gestureDuration = 1.4;
        break;
      case 'think':
        this.gestureDuration = 2.0;
        break;
      case 'shrug':
        this.gestureDuration = 1.6;
        break;
      case 'wave':
        this.gestureDuration = 2.0;
        break;
      case 'laugh':
        this.gestureDuration = 1.8;
        break;
      case 'facepalm':
        this.gestureDuration = 2.2;
        break;
      case 'surprise':
        this.gestureDuration = 1.2;
        break;
      default:
        this.gestureDuration = 1.0;
    }
  }

  update(delta) {
    if (!this.activeGesture || !this.vrm || !this.vrm.humanoid) return;

    this.gestureTimer += delta;
    const progress = Math.min(1.0, this.gestureTimer / this.gestureDuration);

    const head = this.vrm.humanoid.getNormalizedBoneNode('head');
    const neck = this.vrm.humanoid.getNormalizedBoneNode('neck');
    const chest = this.vrm.humanoid.getNormalizedBoneNode('chest');
    const leftShoulder = this.vrm.humanoid.getNormalizedBoneNode('leftShoulder');
    const rightShoulder = this.vrm.humanoid.getNormalizedBoneNode('rightShoulder');
    const rightUpperArm = this.vrm.humanoid.getNormalizedBoneNode('rightUpperArm');
    const rightLowerArm = this.vrm.humanoid.getNormalizedBoneNode('rightLowerArm');
    const rightHand = this.vrm.humanoid.getNormalizedBoneNode('rightHand');

    // Smooth bell curve envelope (rises from 0 to 1 and back to 0)
    const envelope = Math.sin(progress * Math.PI);

    switch (this.activeGesture) {
      case 'nod': {
        const nodSin = Math.sin(progress * Math.PI * 4); // 2 smooth polite nods
        if (head) head.rotation.x += nodSin * 0.11 * envelope;
        if (neck) neck.rotation.x += nodSin * 0.05 * envelope;
        break;
      }

      case 'shake': {
        const shakeSin = Math.sin(progress * Math.PI * 5); // expressive "no"
        if (head) head.rotation.y += shakeSin * 0.15 * envelope;
        if (neck) neck.rotation.y += shakeSin * 0.05 * envelope;
        break;
      }

      case 'think': {
        // Pensive tilt and hand toward chin
        if (head) {
          head.rotation.z += 0.09 * envelope;
          head.rotation.x -= 0.06 * envelope;
        }
        if (rightUpperArm) {
          rightUpperArm.rotation.z += 0.85 * envelope; // raise arm up towards chest
          rightUpperArm.rotation.x += 0.40 * envelope;
          rightUpperArm.rotation.y += 0.25 * envelope;
        }
        if (rightLowerArm) {
          rightLowerArm.rotation.z += 0.65 * envelope;
          rightLowerArm.rotation.y += 0.40 * envelope;
        }
        break;
      }

      case 'shrug': {
        // Shoulders lift upward naturally, arms open slightly
        const shrugLift = 0.12 * envelope;
        if (leftShoulder) leftShoulder.rotation.z += shrugLift;
        if (rightShoulder) rightShoulder.rotation.z -= shrugLift;
        if (leftUpperArm) leftUpperArm.rotation.z -= 0.15 * envelope;
        if (rightUpperArm) rightUpperArm.rotation.z += 0.15 * envelope;
        if (head) head.rotation.z += Math.sin(progress * Math.PI * 2) * 0.04 * envelope;
        break;
      }

      case 'wave': {
        // Raise right arm up to wave level (positive Z in three-vrm)
        if (rightUpperArm) {
          rightUpperArm.rotation.z += 1.45 * envelope;
          rightUpperArm.rotation.x += 0.25 * envelope;
        }
        if (rightLowerArm) {
          rightLowerArm.rotation.z += 0.55 * envelope;
          rightLowerArm.rotation.y += 0.35 * envelope;
        }
        if (rightHand) {
          const waveSin = Math.sin(progress * Math.PI * 8);
          rightHand.rotation.z += waveSin * 0.22 * envelope;
        }
        break;
      }

      case 'laugh': {
        const laughVibe = Math.sin(progress * Math.PI * 12) * 0.025;
        if (head) head.rotation.x += (-0.05 + laughVibe) * envelope;
        if (neck) neck.rotation.x += laughVibe * 0.5 * envelope;
        if (chest) chest.rotation.x += laughVibe * 0.7 * envelope;
        break;
      }

      case 'facepalm': {
        // Hand to forehead, head bowed in disbelief
        if (head) head.rotation.x += 0.14 * envelope;
        if (rightUpperArm) {
          rightUpperArm.rotation.z += 1.25 * envelope; // lift right arm up towards face
          rightUpperArm.rotation.x += 0.55 * envelope;
          rightUpperArm.rotation.y += 0.35 * envelope;
        }
        if (rightLowerArm) {
          rightLowerArm.rotation.z += 0.90 * envelope;
          rightLowerArm.rotation.y += 0.50 * envelope;
        }
        break;
      }

      case 'surprise': {
        if (head) head.rotation.x -= 0.09 * envelope;
        if (leftShoulder) leftShoulder.rotation.z += 0.08 * envelope;
        if (rightShoulder) rightShoulder.rotation.z -= 0.08 * envelope;
        break;
      }
    }

    if (progress >= 1.0) {
      this.activeGesture = null;
    }
  }
}
