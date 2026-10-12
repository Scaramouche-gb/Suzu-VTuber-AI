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
    const leftShoulder = this.vrm.humanoid.getNormalizedBoneNode('leftShoulder');
    const rightShoulder = this.vrm.humanoid.getNormalizedBoneNode('rightShoulder');
    const rightUpperArm = this.vrm.humanoid.getNormalizedBoneNode('rightUpperArm');
    const rightHand = this.vrm.humanoid.getNormalizedBoneNode('rightHand');

    // Envelope: rise and fall smoothly
    const envelope = Math.sin(progress * Math.PI);

    switch (this.activeGesture) {
      case 'nod': {
        const nodSin = Math.sin(progress * Math.PI * 4); // 2 nods
        if (head) head.rotation.x += nodSin * 0.18 * envelope;
        if (neck) neck.rotation.x += nodSin * 0.08 * envelope;
        break;
      }

      case 'shake': {
        const shakeSin = Math.sin(progress * Math.PI * 5); // head shake
        if (head) head.rotation.y += shakeSin * 0.22 * envelope;
        break;
      }

      case 'think': {
        // Head tilted, looking slightly away
        if (head) {
          head.rotation.z += 0.14 * envelope;
          head.rotation.x -= 0.10 * envelope;
        }
        break;
      }

      case 'shrug': {
        const shrugLift = 0.22 * envelope;
        if (leftShoulder) leftShoulder.position.y += shrugLift * 0.05;
        if (rightShoulder) rightShoulder.position.y += shrugLift * 0.05;
        if (head) head.rotation.z += Math.sin(progress * Math.PI) * 0.08;
        break;
      }

      case 'wave': {
        if (rightUpperArm) {
          rightUpperArm.rotation.z = -1.2 * envelope;
          rightUpperArm.rotation.x = -0.4 * envelope;
        }
        if (rightHand) {
          const waveSin = Math.sin(progress * Math.PI * 8);
          rightHand.rotation.z = waveSin * 0.35 * envelope;
        }
        break;
      }

      case 'laugh': {
        const laughVibe = Math.sin(progress * Math.PI * 12) * 0.04;
        if (head) head.rotation.x += (-0.12 + laughVibe) * envelope;
        break;
      }

      case 'facepalm': {
        if (head) head.rotation.x += 0.22 * envelope;
        if (rightUpperArm) {
          rightUpperArm.rotation.z = -0.8 * envelope;
          rightUpperArm.rotation.x = -1.1 * envelope;
        }
        break;
      }

      case 'surprise': {
        if (head) head.rotation.x -= 0.15 * envelope;
        break;
      }
    }

    if (progress >= 1.0) {
      this.activeGesture = null;
    }
  }
}
