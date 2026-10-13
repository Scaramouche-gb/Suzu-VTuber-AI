import * as THREE from 'three';
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';
import { VRMLoaderPlugin, VRMUtils } from '@pixiv/three-vrm';
import { VRMAnimationLoaderPlugin } from '@pixiv/three-vrm-animation';

export class AvatarController {
  constructor(canvas) {
    this.canvas = canvas;
    this.renderer = null;
    this.scene = null;
    this.camera = null;
    this.vrm = null;
    this.mixer = null;
    this.lookTarget = null;
    this.clock = new THREE.Clock();

    // Natural life signs & procedural motion timers
    this.blinkTimer = 0.0;
    this.nextBlinkInterval = 3.0;
    this.isBlinking = false;
    this.blinkProgress = 0.0;

    this.breathTime = 0.0;
    this.swayTime = 0.0;
    this.headNoiseTime = 0.0;
    this.speakTime = 0.0;

    // Interactive state: 'idle' | 'listening' | 'thinking' | 'speaking'
    this.state = 'idle';
    this.speechVolume = 0.0; // Fed from LipsyncManager for conversational sync

    // LookAt & Eye Tracking
    this.cursorPosition = new THREE.Vector2(0, 0);
    this.targetLookPos = new THREE.Vector3(0, 1.30, 2.0);
    this.currentLookPos = new THREE.Vector3(0, 1.30, 2.0);
    this.saccadeTimer = 0.0;
    this.nextSaccade = 2.0;
    this.saccadeOffset = new THREE.Vector3();

    // Dynamic camera config
    this.scale = 1.0;
    this.yOffset = 0.0;
    this.fitMode = 'bust';
    this.hasMocapIdle = false;

    this.initScene();
    this.initEvents();
  }

  initScene() {
    this.renderer = new THREE.WebGLRenderer({
      canvas: this.canvas,
      alpha: true,
      antialias: true,
      premultipliedAlpha: true,
    });
    this.renderer.setClearColor(0x000000, 0);
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;

    this.scene = new THREE.Scene();

    const aspect = window.innerWidth / window.innerHeight;
    this.camera = new THREE.PerspectiveCamera(24, aspect, 0.1, 20);
    this.updateCameraPlacement();

    // Warm, flattering 3-point anime lighting setup
    const keyLight = new THREE.DirectionalLight(0xfff6ee, Math.PI * 0.95);
    keyLight.position.set(0.8, 1.8, 1.6).normalize();
    this.scene.add(keyLight);

    const fillLight = new THREE.DirectionalLight(0xe8f0ff, Math.PI * 0.45);
    fillLight.position.set(-1.0, 1.2, 1.0).normalize();
    this.scene.add(fillLight);

    const rimLight = new THREE.DirectionalLight(0xfff0ff, Math.PI * 0.35);
    rimLight.position.set(0.0, 2.0, -1.5).normalize();
    this.scene.add(rimLight);

    const ambientLight = new THREE.AmbientLight(0xffffff, 0.65);
    this.scene.add(ambientLight);

    this.lookTarget = new THREE.Object3D();
    this.lookTarget.position.copy(this.currentLookPos);
    this.scene.add(this.lookTarget);

    this.onWindowResize();
  }

  updateCameraPlacement() {
    if (this.fitMode === 'bust') {
      // Warm, intimate framing of head, neck, shoulders, and chest
      this.camera.position.set(0, 1.15 + this.yOffset, 1.82 / this.scale);
      this.camera.lookAt(0, 1.10 + this.yOffset, 0);
    } else {
      this.camera.position.set(0, 0.75 + this.yOffset, 3.2 / this.scale);
      this.camera.lookAt(0, 0.70 + this.yOffset, 0);
    }
  }

  initEvents() {
    window.addEventListener('resize', () => this.onWindowResize());
    window.addEventListener('mousemove', (e) => {
      this.cursorPosition.x = (e.clientX / window.innerWidth) * 2 - 1;
      this.cursorPosition.y = -(e.clientY / window.innerHeight) * 2 + 1;

      this.targetLookPos.set(
        this.cursorPosition.x * 0.40,
        1.30 + this.cursorPosition.y * 0.22 + this.yOffset,
        2.0
      );
    });
    window.addEventListener('keydown', (e) => {
      if (e.key === 'f' || e.key === 'F') {
        this.flip();
      }
    });
  }

  onWindowResize() {
    const width = window.innerWidth;
    const height = window.innerHeight;
    this.camera.aspect = width / height;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(width, height);
  }

  async loadModel(url) {
    const loader = new GLTFLoader();
    loader.register((parser) => new VRMLoaderPlugin(parser));
    loader.register((parser) => new VRMAnimationLoaderPlugin(parser));

    const gltf = await loader.loadAsync(url);
    const vrm = gltf.userData.vrm;
    if (!vrm) {
      throw new Error('GLTF model is missing VRM metadata');
    }

    VRMUtils.removeUnnecessaryVertices(gltf.scene);
    VRMUtils.combineSkeletons(gltf.scene);

    if (this.vrm) {
      this.scene.remove(this.vrm.scene);
    }

    this.vrm = vrm;
    this.scene.add(vrm.scene);

    if (vrm.lookAt) {
      vrm.lookAt.target = this.lookTarget;
      vrm.lookAt.autoUpdate = true;
    }

    this.mixer = new THREE.AnimationMixer(vrm.scene);
    vrm.scene.rotation.y = Math.PI;
    return vrm;
  }

  flip() {
    if (this.vrm && this.vrm.scene) {
      this.vrm.scene.rotation.y = (this.vrm.scene.rotation.y + Math.PI) % (Math.PI * 2);
    }
  }

  setState(newState) {
    this.state = newState;
  }

  setSpeechVolume(vol) {
    this.speechVolume = vol;
  }

  setMocapIdleActive(active) {
    this.hasMocapIdle = Boolean(active);
  }

  preparePose(delta) {
    if (!this.vrm) return;

    if (!this.hasMocapIdle) {
      if (this.vrm.humanoid) {
        this.vrm.humanoid.resetNormalizedPose();
      }
      this.applyIdlePose(delta);
    }
  }

  finishFrame(delta) {
    if (!this.vrm) return;

    // 1. Natural blinking with organic double-blink variation
    this.updateBlinking(delta);

    // 2. Smooth LookAt interpolation with subtle ocular saccades
    this.saccadeTimer += delta;
    if (this.saccadeTimer >= this.nextSaccade) {
      this.saccadeTimer = 0.0;
      this.nextSaccade = 1.6 + Math.random() * 2.8;
      // Quick subtle shift in eye gaze point
      this.saccadeOffset.set(
        (Math.random() - 0.5) * 0.045,
        (Math.random() - 0.5) * 0.025,
        0
      );
    }
    const targetWithDrift = this.targetLookPos.clone().add(this.saccadeOffset);
    this.currentLookPos.lerp(targetWithDrift, Math.min(1.0, delta * 5.0));
    this.lookTarget.position.copy(this.currentLookPos);

    // 3. Animation mixer update
    if (this.mixer) {
      this.mixer.update(delta);
    }

    // 4. Spring bone physics simulation (hair, ribbons, clothes)
    this.vrm.update(delta);

    // 5. Render
    this.renderer.render(this.scene, this.camera);
  }

  update(delta) {
    this.preparePose(delta);
    this.finishFrame(delta);
  }

  applyIdlePose(delta) {
    if (!this.vrm || !this.vrm.humanoid) return;

    const humanoid = this.vrm.humanoid;
    this.breathTime += delta;
    this.swayTime += delta * 0.55;
    this.headNoiseTime += delta * 0.70;

    // 1. Multi-harmonic organic breathing (primary ~3.8s, secondary ~7.2s)
    const breathPrimary = Math.sin((this.breathTime * Math.PI * 2) / 3.8);
    const breathSecondary = Math.sin((this.breathTime * Math.PI * 2) / 7.2) * 0.35;
    const breath = breathPrimary + breathSecondary;
    const breathAngle = breath * 0.022;

    // 2. Organic weight shifting (Lissajous figure-8 curve for pelvis/hips)
    const hipLateral = Math.sin(this.swayTime) * 0.024;
    const hipTwist = Math.cos(this.swayTime * 0.5) * 0.015;
    const spineCounter = -hipLateral * 0.60;
    const chestCounter = -hipLateral * 0.35;

    // Hips / Pelvis
    const hips = humanoid.getNormalizedBoneNode('hips');
    if (hips) {
      hips.rotation.z = hipLateral;
      hips.rotation.y = hipTwist;
    }

    // Spine & Chest
    const spine = humanoid.getNormalizedBoneNode('spine');
    const chest = humanoid.getNormalizedBoneNode('chest');
    const upperChest = humanoid.getNormalizedBoneNode('upperChest');

    if (spine) {
      spine.rotation.set(-0.012 + breathAngle * 0.35, 0, spineCounter);
    }
    if (chest) {
      chest.rotation.set(0.018 + breathAngle * 0.85, 0, chestCounter);
    }
    if (upperChest) {
      upperChest.rotation.set(0.010 + breathAngle * 0.30, 0, 0);
    }

    // 3. Head & Neck multi-harmonic micro-motion + Conversational rhythm
    const head = humanoid.getNormalizedBoneNode('head');
    const neck = humanoid.getNormalizedBoneNode('neck');

    // Layered prime harmonics for organic non-repeating head drift
    let microPitch =
      Math.sin(this.headNoiseTime * 0.82) * 0.020 +
      Math.sin(this.headNoiseTime * 1.63) * 0.010 +
      Math.sin(this.headNoiseTime * 0.31) * 0.012;
    let microYaw =
      Math.sin(this.headNoiseTime * 0.64) * 0.025 +
      Math.cos(this.headNoiseTime * 1.18) * 0.012;
    let microRoll =
      Math.sin(this.headNoiseTime * 0.51) * 0.015 + spineCounter * 0.35;

    // Conversational dynamics & reactive states
    if (this.state === 'listening') {
      // Inquisitive, attentive posture
      microPitch -= 0.040;
      microRoll += 0.055;
      microYaw -= 0.025;
    } else if (this.state === 'thinking') {
      // Pensive gaze upward-left
      microPitch -= 0.065;
      microYaw += 0.090;
      microRoll += 0.080;
    } else if (this.state === 'speaking') {
      // Lively conversational nodding sync'd with speech cadence
      this.speakTime += delta * 3.6;
      const speechPulse = this.speechVolume * 0.05;
      microPitch += Math.sin(this.speakTime) * 0.028 + speechPulse;
      microYaw += Math.cos(this.speakTime * 0.5) * 0.022;
      microRoll += Math.sin(this.speakTime * 0.7) * 0.016;
    }

    if (head) {
      head.rotation.set(microPitch, microYaw, microRoll);
    }
    if (neck) {
      neck.rotation.set(microPitch * 0.35, microYaw * 0.35, microRoll * 0.25);
    }

    // 4. Natural relaxed arm & cute soft hand resting posture
    const leftUpperArm = humanoid.getNormalizedBoneNode('leftUpperArm');
    const leftLowerArm = humanoid.getNormalizedBoneNode('leftLowerArm');
    const leftHand = humanoid.getNormalizedBoneNode('leftHand');
    const leftShoulder = humanoid.getNormalizedBoneNode('leftShoulder');

    const rightUpperArm = humanoid.getNormalizedBoneNode('rightUpperArm');
    const rightLowerArm = humanoid.getNormalizedBoneNode('rightLowerArm');
    const rightHand = humanoid.getNormalizedBoneNode('rightHand');
    const rightShoulder = humanoid.getNormalizedBoneNode('rightShoulder');

    const armSway = hipLateral * 0.35;

    // Left Arm: gracefully hangs down along the torso, slight forward tilt, soft elbow bend
    if (leftUpperArm) {
      leftUpperArm.rotation.set(
        0.14 + Math.sin(this.swayTime) * 0.015,
        0.04,
        1.28 - breathAngle * 0.25 + armSway
      );
    }
    if (leftLowerArm) {
      leftLowerArm.rotation.set(0.24, 0.05, 0.10);
    }
    if (leftHand) {
      leftHand.rotation.set(0.06, 0.02, 0.04);
    }
    if (leftShoulder) {
      leftShoulder.rotation.set(0.0, 0.0, 0.03 + breathAngle * 0.22);
    }

    // Right Arm: gracefully hangs down symmetrically
    if (rightUpperArm) {
      rightUpperArm.rotation.set(
        0.14 - Math.sin(this.swayTime) * 0.015,
        -0.04,
        -1.28 + breathAngle * 0.25 + armSway
      );
    }
    if (rightLowerArm) {
      rightLowerArm.rotation.set(0.24, -0.05, -0.10);
    }
    if (rightHand) {
      rightHand.rotation.set(0.06, -0.02, -0.04);
    }
    if (rightShoulder) {
      rightShoulder.rotation.set(0.0, 0.0, -0.03 - breathAngle * 0.22);
    }

    // 5. Soft, organic resting finger curl (replaces rigid flat T-pose paddles!)
    this.applyRelaxedFingers(humanoid);
  }

  applyRelaxedFingers(humanoid) {
    // Curl values for cute relaxed anime hand resting pose
    const fingerBones = [
      ['leftThumbProximal', 0.12, 0.05, 0.08],
      ['leftThumbIntermediate', 0.15, 0.0, 0.0],
      ['leftIndexProximal', 0.22, 0.0, 0.05],
      ['leftIndexIntermediate', 0.28, 0.0, 0.0],
      ['leftMiddleProximal', 0.25, 0.0, 0.02],
      ['leftMiddleIntermediate', 0.32, 0.0, 0.0],
      ['leftRingProximal', 0.26, 0.0, -0.02],
      ['leftRingIntermediate', 0.34, 0.0, 0.0],
      ['leftLittleProximal', 0.24, 0.0, -0.05],
      ['leftLittleIntermediate', 0.30, 0.0, 0.0],

      ['rightThumbProximal', 0.12, -0.05, -0.08],
      ['rightThumbIntermediate', 0.15, 0.0, 0.0],
      ['rightIndexProximal', 0.22, 0.0, -0.05],
      ['rightIndexIntermediate', 0.28, 0.0, 0.0],
      ['rightMiddleProximal', 0.25, 0.0, -0.02],
      ['rightMiddleIntermediate', 0.32, 0.0, 0.0],
      ['rightRingProximal', 0.26, 0.0, 0.02],
      ['rightRingIntermediate', 0.34, 0.0, 0.0],
      ['rightLittleProximal', 0.24, 0.0, 0.05],
      ['rightLittleIntermediate', 0.30, 0.0, 0.0],
    ];

    for (const [boneName, rx, ry, rz] of fingerBones) {
      const node = humanoid.getNormalizedBoneNode(boneName);
      if (node) {
        node.rotation.set(rx, ry, rz);
      }
    }
  }

  updateBlinking(delta) {
    if (!this.vrm || !this.vrm.expressionManager) return;

    this.blinkTimer += delta;
    if (!this.isBlinking && this.blinkTimer >= this.nextBlinkInterval) {
      this.isBlinking = true;
      this.blinkProgress = 0.0;
      this.blinkTimer = 0.0;
      // 22% chance of natural double-blink
      this.nextBlinkInterval = Math.random() < 0.22 ? 0.32 : 2.5 + Math.random() * 3.5;
    }

    if (this.isBlinking) {
      this.blinkProgress += delta / 0.15; // ~150ms natural blink duration
      let weight = 0.0;
      if (this.blinkProgress <= 0.45) {
        // Fast snap close
        weight = this.blinkProgress / 0.45;
      } else if (this.blinkProgress <= 1.0) {
        // Gentle spring open
        weight = 1.0 - (this.blinkProgress - 0.45) / 0.55;
      } else {
        this.isBlinking = false;
        weight = 0.0;
      }
      try {
        this.vrm.expressionManager.setValue('blink', weight);
      } catch {}
    }
  }

  setFit(mode, scale = 1.0, yOffset = 0.0) {
    this.fitMode = mode;
    this.scale = scale;
    this.yOffset = yOffset;
    this.updateCameraPlacement();
  }

  setLookTarget(mode, x = 0.5, y = 0.3) {
    if (mode === 'screen') {
      this.targetLookPos.set(0.12, 1.15 + this.yOffset, 1.5);
    } else if (mode === 'user') {
      this.targetLookPos.set(0.0, 1.30 + this.yOffset, 2.0);
    } else if (mode === 'point') {
      this.targetLookPos.set((x - 0.5) * 0.8, 1.30 + (0.5 - y) * 0.5 + this.yOffset, 1.8);
    }
  }
}
