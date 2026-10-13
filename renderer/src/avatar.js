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

    // Life signs & procedural motion state
    this.blinkTimer = 0;
    this.nextBlinkInterval = 3.2;
    this.isBlinking = false;
    this.blinkProgress = 0;

    this.breathTime = 0;
    this.headNoiseTime = 0;
    this.swayTime = 0;
    this.speakTime = 0;

    // Interactive state: 'idle' | 'listening' | 'thinking' | 'speaking'
    this.state = 'idle';

    // Target positions
    this.cursorPosition = new THREE.Vector2(0, 0);
    this.targetLookPos = new THREE.Vector3(0, 1.30, 2.0);
    this.currentLookPos = new THREE.Vector3(0, 1.30, 2.0);

    // Dynamic config
    this.scale = 1.0;
    this.yOffset = 0.0;
    this.fitMode = 'bust'; // 'bust' or 'full'

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
    this.camera = new THREE.PerspectiveCamera(25, aspect, 0.1, 20);
    this.updateCameraPlacement();

    // Lighting
    const directionalLight = new THREE.DirectionalLight(0xffffff, Math.PI * 0.95);
    directionalLight.position.set(1.0, 1.6, 1.2).normalize();
    this.scene.add(directionalLight);

    const ambientLight = new THREE.AmbientLight(0xffffff, 0.75);
    this.scene.add(ambientLight);

    this.lookTarget = new THREE.Object3D();
    this.lookTarget.position.copy(this.currentLookPos);
    this.scene.add(this.lookTarget);

    this.onWindowResize();
  }

  updateCameraPlacement() {
    if (this.fitMode === 'bust') {
      // Focus on bust (chest, shoulders, head nicely framed with breathing room)
      this.camera.position.set(0, 1.16 + this.yOffset, 1.85 / this.scale);
      this.camera.lookAt(0, 1.10 + this.yOffset, 0);
    } else {
      // Full body view
      this.camera.position.set(0, 0.75 + this.yOffset, 3.2 / this.scale);
      this.camera.lookAt(0, 0.70 + this.yOffset, 0);
    }
  }

  initEvents() {
    window.addEventListener('resize', () => this.onWindowResize());
    window.addEventListener('mousemove', (e) => {
      this.cursorPosition.x = (e.clientX / window.innerWidth) * 2 - 1;
      this.cursorPosition.y = -(e.clientY / window.innerHeight) * 2 + 1;

      // Project onto comfortable look target cone in front of head
      this.targetLookPos.set(
        this.cursorPosition.x * 0.45,
        1.30 + this.cursorPosition.y * 0.25 + this.yOffset,
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

    // Initial setup: face camera (+Z)
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

  preparePose(delta) {
    if (!this.vrm) return;

    // 1. Reset normalized humanoid pose
    if (this.vrm.humanoid) {
      this.vrm.humanoid.resetNormalizedPose();
    }

    // 2. Apply rich procedural life pose (sway, breathing, reactive state)
    this.applyIdlePose(delta);
  }

  finishFrame(delta) {
    if (!this.vrm) return;

    // 3. Life signs: Blinking
    this.updateBlinking(delta);

    // 4. Smooth LookAt interpolation with subtle saccadic micro-drift
    const saccadeX = Math.sin(this.breathTime * 1.7) * 0.015;
    const saccadeY = Math.cos(this.breathTime * 1.3) * 0.01;
    const driftedLook = this.targetLookPos.clone().add(new THREE.Vector3(saccadeX, saccadeY, 0));
    this.currentLookPos.lerp(driftedLook, Math.min(1.0, delta * 5.0));
    this.lookTarget.position.copy(this.currentLookPos);

    // 5. Animation mixer
    if (this.mixer) {
      this.mixer.update(delta);
    }

    // 6. Spring bones physics (hair, ribbons, clothes)
    this.vrm.update(delta);

    // 7. Render
    this.renderer.render(this.scene, this.camera);
  }

  update(delta) {
    this.preparePose(delta);
    this.finishFrame(delta);
  }

  applyIdlePose(delta) {
    if (!this.vrm || !this.vrm.humanoid) return;

    this.breathTime += delta;
    this.swayTime += delta * 0.6;
    this.headNoiseTime += delta * 0.75;

    // 1. Natural respiration cycle (~4.2s per full breath)
    const breathCycle = Math.sin((this.breathTime * Math.PI * 2) / 4.2);
    const breathAngle = breathCycle * 0.02;

    // 2. Organic weight shifting / hip & spine lateral sway (~7s cycle)
    const hipSway = Math.sin(this.swayTime) * 0.026;
    const spineCounterSway = -hipSway * 0.65;
    const chestCounterSway = -hipSway * 0.35;

    // Hips / Pelvis
    const hips = this.vrm.humanoid.getNormalizedBoneNode('hips');
    if (hips) {
      hips.rotation.z = hipSway;
      hips.rotation.y = Math.cos(this.swayTime * 0.5) * 0.015;
    }

    // Spine & chest
    const spine = this.vrm.humanoid.getNormalizedBoneNode('spine');
    const chest = this.vrm.humanoid.getNormalizedBoneNode('chest');
    const upperChest = this.vrm.humanoid.getNormalizedBoneNode('upperChest');

    if (spine) {
      spine.rotation.set(-0.01 + breathAngle * 0.4, 0, spineCounterSway);
    }
    if (chest) {
      chest.rotation.set(0.015 + breathAngle * 0.8, 0, chestCounterSway);
    }
    if (upperChest) {
      upperChest.rotation.set(0.01 + breathAngle * 0.3, 0, 0);
    }

    // 3. Head & neck micro-motion with reactive state
    const head = this.vrm.humanoid.getNormalizedBoneNode('head');
    const neck = this.vrm.humanoid.getNormalizedBoneNode('neck');

    let microYaw = Math.sin(this.headNoiseTime * 1.1) * 0.028;
    let microPitch = Math.sin(this.headNoiseTime * 0.85 + 1.2) * 0.02;
    let microRoll = Math.sin(this.headNoiseTime * 0.65) * 0.016 + spineCounterSway * 0.4;

    // Reactive posture adjustments
    if (this.state === 'listening') {
      microPitch -= 0.035; // Head tilted slightly up towards user
      microRoll += 0.045;  // Inquisitive head tilt
    } else if (this.state === 'thinking') {
      microPitch -= 0.055; // Pensive upward gaze
      microYaw += 0.08;
      microRoll += 0.07;
    } else if (this.state === 'speaking') {
      this.speakTime += delta * 3.8;
      microPitch += Math.sin(this.speakTime) * 0.024; // Conversational rhythm nod
      microYaw += Math.cos(this.speakTime * 0.5) * 0.018;
    }

    if (head) {
      head.rotation.set(microPitch, microYaw, microRoll);
    }
    if (neck) {
      neck.rotation.set(microPitch * 0.35, microYaw * 0.35, microRoll * 0.25);
    }

    // 4. Natural arm relaxation and coordination with breathing & hip sway
    const leftUpperArm = this.vrm.humanoid.getNormalizedBoneNode('leftUpperArm');
    const leftLowerArm = this.vrm.humanoid.getNormalizedBoneNode('leftLowerArm');
    const leftHand = this.vrm.humanoid.getNormalizedBoneNode('leftHand');
    const leftShoulder = this.vrm.humanoid.getNormalizedBoneNode('leftShoulder');

    const armSway = hipSway * 0.4;

    if (leftUpperArm) {
      leftUpperArm.rotation.set(
        0.08 + Math.sin(this.swayTime) * 0.02,
        0.05,
        1.25 - breathAngle * 0.3 + armSway
      );
    }
    if (leftLowerArm) {
      leftLowerArm.rotation.set(0.18, 0.0, 0.12);
    }
    if (leftHand) {
      leftHand.rotation.set(0.05, 0.0, 0.05);
    }
    if (leftShoulder) {
      leftShoulder.rotation.set(0.0, 0.0, 0.04 + breathAngle * 0.2);
    }

    const rightUpperArm = this.vrm.humanoid.getNormalizedBoneNode('rightUpperArm');
    const rightLowerArm = this.vrm.humanoid.getNormalizedBoneNode('rightLowerArm');
    const rightHand = this.vrm.humanoid.getNormalizedBoneNode('rightHand');
    const rightShoulder = this.vrm.humanoid.getNormalizedBoneNode('rightShoulder');

    if (rightUpperArm) {
      rightUpperArm.rotation.set(
        0.08 - Math.sin(this.swayTime) * 0.02,
        -0.05,
        -1.25 + breathAngle * 0.3 + armSway
      );
    }
    if (rightLowerArm) {
      rightLowerArm.rotation.set(0.18, 0.0, -0.12);
    }
    if (rightHand) {
      rightHand.rotation.set(0.05, 0.0, -0.05);
    }
    if (rightShoulder) {
      rightShoulder.rotation.set(0.0, 0.0, -0.04 - breathAngle * 0.2);
    }
  }

  updateBlinking(delta) {
    if (!this.vrm || !this.vrm.expressionManager) return;

    this.blinkTimer += delta;
    if (!this.isBlinking && this.blinkTimer >= this.nextBlinkInterval) {
      this.isBlinking = true;
      this.blinkProgress = 0;
      this.blinkTimer = 0;
      this.nextBlinkInterval = 2.2 + Math.random() * 3.8;
    }

    if (this.isBlinking) {
      this.blinkProgress += delta / 0.16; // ~160ms natural blink duration
      let weight = 0;
      if (this.blinkProgress <= 0.5) {
        weight = this.blinkProgress * 2;
      } else if (this.blinkProgress <= 1.0) {
        weight = (1.0 - this.blinkProgress) * 2;
      } else {
        this.isBlinking = false;
        weight = 0;
      }
      try {
        this.vrm.expressionManager.setValue('blink', weight);
      } catch {
        // Fallback if blink name is different
      }
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
      this.targetLookPos.set(0.1, 1.15 + this.yOffset, 1.5);
    } else if (mode === 'user') {
      this.targetLookPos.set(0, 1.30 + this.yOffset, 2.0);
    } else if (mode === 'point') {
      this.targetLookPos.set((x - 0.5) * 0.8, 1.30 + (0.5 - y) * 0.5 + this.yOffset, 1.8);
    }
  }
}
