/**
 * Lifelike Procedural Gesture & Body Language Engine.
 * Provides organic full-body coordination (spine, chest, head, shoulders, arms, wrists, fingers)
 * with smooth cubic spring envelopes and zero popping.
 */

export class GestureManager {
  constructor(vrm = null, mixer = null) {
    this.vrm = vrm;
    this.mixer = mixer;
    this.activeGesture = null;
    this.gestureTimer = 0.0;
    this.gestureDuration = 1.0;
    this.currentBlend = 0.0;
  }

  setVRM(vrm, mixer) {
    this.vrm = vrm;
    this.mixer = mixer;
  }

  playGesture(name) {
    if (!this.vrm) return;
    const cleanName = (name || '').toLowerCase().trim();
    if (!cleanName) return;

    this.activeGesture = cleanName;
    this.gestureTimer = 0.0;

    switch (cleanName) {
      case 'nod':
        this.gestureDuration = 1.3;
        break;
      case 'shake':
        this.gestureDuration = 1.5;
        break;
      case 'think':
        this.gestureDuration = 2.4;
        break;
      case 'shrug':
        this.gestureDuration = 1.8;
        break;
      case 'wave':
        this.gestureDuration = 2.2;
        break;
      case 'laugh':
        this.gestureDuration = 2.0;
        break;
      case 'facepalm':
        this.gestureDuration = 2.4;
        break;
      case 'surprise':
        this.gestureDuration = 1.3;
        break;
      default:
        this.gestureDuration = 1.2;
    }
  }

  update(delta) {
    if (!this.activeGesture || !this.vrm || !this.vrm.humanoid) {
      this.currentBlend = Math.max(0.0, this.currentBlend - delta * 6.0);
      return;
    }

    this.gestureTimer += delta;
    const progress = Math.min(1.0, this.gestureTimer / this.gestureDuration);

    // Natural attack, sustain, and decay envelope:
    // Ease in (0..0.25), sustain peak (0.25..0.75), smooth ease out (0.75..1.0)
    let envelope = 0.0;
    if (progress < 0.25) {
      const t = progress / 0.25;
      envelope = t * t * (3 - 2 * t); // Smoothstep in
    } else if (progress < 0.75) {
      envelope = 1.0; // Sustain
    } else {
      const t = (1.0 - progress) / 0.25;
      envelope = t * t * (3 - 2 * t); // Smoothstep out
    }
    this.currentBlend = envelope;

    const humanoid = this.vrm.humanoid;
    const head = humanoid.getNormalizedBoneNode('head');
    const neck = humanoid.getNormalizedBoneNode('neck');
    const chest = humanoid.getNormalizedBoneNode('chest');
    const spine = humanoid.getNormalizedBoneNode('spine');

    const leftShoulder = humanoid.getNormalizedBoneNode('leftShoulder');
    const rightShoulder = humanoid.getNormalizedBoneNode('rightShoulder');

    const leftUpperArm = humanoid.getNormalizedBoneNode('leftUpperArm');
    const leftLowerArm = humanoid.getNormalizedBoneNode('leftLowerArm');
    const leftHand = humanoid.getNormalizedBoneNode('leftHand');

    const rightUpperArm = humanoid.getNormalizedBoneNode('rightUpperArm');
    const rightLowerArm = humanoid.getNormalizedBoneNode('rightLowerArm');
    const rightHand = humanoid.getNormalizedBoneNode('rightHand');

    // Fingers for natural gesture poses
    const rIndexProx = humanoid.getNormalizedBoneNode('rightIndexProximal');
    const rMiddleProx = humanoid.getNormalizedBoneNode('rightMiddleProximal');
    const rThumbProx = humanoid.getNormalizedBoneNode('rightThumbProximal');

    const lIndexProx = humanoid.getNormalizedBoneNode('leftIndexProximal');
    const lMiddleProx = humanoid.getNormalizedBoneNode('leftMiddleProximal');

    switch (this.activeGesture) {
      case 'nod': {
        // Natural double conversational nod with neck and upper spine follow-through
        const nodWave = Math.sin(progress * Math.PI * 4);
        const nodDamp = Math.max(0.0, 1.0 - progress * 0.4);
        if (head) head.rotation.x += nodWave * 0.12 * envelope * nodDamp;
        if (neck) neck.rotation.x += nodWave * 0.06 * envelope * nodDamp;
        if (chest) chest.rotation.x += nodWave * 0.03 * envelope * nodDamp;
        break;
      }

      case 'shake': {
        // Expressive disagreement or banter head shake with playful chin tilt
        const shakeWave = Math.sin(progress * Math.PI * 5);
        if (head) {
          head.rotation.y += shakeWave * 0.14 * envelope;
          head.rotation.z += Math.cos(progress * Math.PI * 5) * 0.03 * envelope;
        }
        if (neck) neck.rotation.y += shakeWave * 0.05 * envelope;
        break;
      }

      case 'think': {
        // Pensive posture: right hand up touching chin, head cocked thoughtfully, gaze up
        if (head) {
          head.rotation.z += 0.08 * envelope;
          head.rotation.x -= 0.05 * envelope;
          head.rotation.y -= 0.06 * envelope;
        }
        if (spine) spine.rotation.y += 0.03 * envelope;

        if (rightShoulder) rightShoulder.rotation.z -= 0.04 * envelope;
        if (rightUpperArm) {
          rightUpperArm.rotation.x += 0.45 * envelope;
          rightUpperArm.rotation.y += 0.35 * envelope;
          rightUpperArm.rotation.z += 0.90 * envelope;
        }
        if (rightLowerArm) {
          rightLowerArm.rotation.x += 0.30 * envelope;
          rightLowerArm.rotation.y += 0.45 * envelope;
          rightLowerArm.rotation.z += 0.75 * envelope;
        }
        if (rightHand) {
          rightHand.rotation.x += 0.15 * envelope;
          rightHand.rotation.z -= 0.10 * envelope;
        }
        if (rIndexProx) rIndexProx.rotation.z -= 0.20 * envelope;
        break;
      }

      case 'shrug': {
        // Both shoulders lift, palms turn out, inquisitive head tilt
        const shrugLift = 0.14 * envelope;
        if (leftShoulder) leftShoulder.rotation.z += shrugLift;
        if (rightShoulder) rightShoulder.rotation.z -= shrugLift;

        if (leftUpperArm) {
          leftUpperArm.rotation.z -= 0.18 * envelope;
          leftUpperArm.rotation.x += 0.10 * envelope;
        }
        if (rightUpperArm) {
          rightUpperArm.rotation.z += 0.18 * envelope;
          rightUpperArm.rotation.x += 0.10 * envelope;
        }

        if (leftLowerArm) leftLowerArm.rotation.z += 0.25 * envelope;
        if (rightLowerArm) rightLowerArm.rotation.z -= 0.25 * envelope;

        if (leftHand) leftHand.rotation.z += 0.20 * envelope;
        if (rightHand) rightHand.rotation.z -= 0.20 * envelope;

        if (head) head.rotation.z += 0.06 * envelope;
        break;
      }

      case 'wave': {
        // Friendly lively hand wave with cheerful wrist rhythm
        if (rightUpperArm) {
          rightUpperArm.rotation.x += 0.35 * envelope;
          rightUpperArm.rotation.y += 0.15 * envelope;
          rightUpperArm.rotation.z += 1.40 * envelope;
        }
        if (rightLowerArm) {
          rightLowerArm.rotation.x += 0.25 * envelope;
          rightLowerArm.rotation.y += 0.35 * envelope;
          rightLowerArm.rotation.z += 0.65 * envelope;
        }
        if (rightHand) {
          const waveRhythm = Math.sin(progress * Math.PI * 9);
          rightHand.rotation.z += waveRhythm * 0.28 * envelope;
          rightHand.rotation.x += 0.10 * envelope;
        }
        if (head) head.rotation.z -= 0.05 * envelope;
        break;
      }

      case 'laugh': {
        // Laughing giggle: chest bounces, head tilts back then forward in amusement
        const giggle = Math.sin(progress * Math.PI * 14) * 0.035;
        if (chest) chest.rotation.x += (-0.03 + giggle) * envelope;
        if (spine) spine.rotation.x += giggle * 0.5 * envelope;
        if (head) {
          head.rotation.x += (-0.07 + giggle * 0.8) * envelope;
          head.rotation.z += 0.03 * envelope;
        }
        if (leftShoulder) leftShoulder.rotation.z += giggle * 0.4 * envelope;
        if (rightShoulder) rightShoulder.rotation.z -= giggle * 0.4 * envelope;
        break;
      }

      case 'facepalm': {
        // Hand to forehead with slight head shake of exasperation
        if (head) {
          head.rotation.x += 0.15 * envelope;
          head.rotation.z += 0.04 * envelope;
        }
        if (chest) chest.rotation.x += 0.04 * envelope;

        if (rightUpperArm) {
          rightUpperArm.rotation.x += 0.55 * envelope;
          rightUpperArm.rotation.y += 0.30 * envelope;
          rightUpperArm.rotation.z += 1.30 * envelope;
        }
        if (rightLowerArm) {
          rightLowerArm.rotation.x += 0.45 * envelope;
          rightLowerArm.rotation.y += 0.50 * envelope;
          rightLowerArm.rotation.z += 1.05 * envelope;
        }
        if (rightHand) {
          rightHand.rotation.x += 0.25 * envelope;
          rightHand.rotation.y += 0.15 * envelope;
        }
        break;
      }

      case 'surprise': {
        // Surprise reaction: chest lifts, head jerks back slightly, shoulders tense
        const surprisePulse = Math.sin(progress * Math.PI);
        if (head) head.rotation.x -= 0.10 * surprisePulse * envelope;
        if (chest) chest.rotation.x -= 0.05 * surprisePulse * envelope;
        if (leftShoulder) leftShoulder.rotation.z += 0.10 * surprisePulse * envelope;
        if (rightShoulder) rightShoulder.rotation.z -= 0.10 * surprisePulse * envelope;
        break;
      }
    }

    if (progress >= 1.0) {
      this.activeGesture = null;
    }
  }
}
