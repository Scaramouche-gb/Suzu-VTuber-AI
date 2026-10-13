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

    // Life signs state
    this.blinkTimer = 0;
    this.nextBlinkInterval = 3.0;
    this.isBlinking = false;
    this.blinkProgress = 0;

    this.breathTime = 0;
    this.headNoiseTime = 0;

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
    const directionalLight = new THREE.DirectionalLight(0xffffff, Math.PI * 0.9);
    directionalLight.position.set(1.0, 1.5, 1.0).normalize();
    this.scene.add(directionalLight);

    const ambientLight = new THREE.AmbientLight(0xffffff, 0.7);
    this.scene.add(ambientLight);

    this.lookTarget = new THREE.Object3D();
    this.lookTarget.position.copy(this.currentLookPos);
    this.scene.add(this.lookTarget);

    this.onWindowResize();
  }

  updateCameraPlacement() {
    if (this.fitMode === 'bust') {
      // Focus on bust (chest, shoulders and head nicely framed with breathing room)
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

    // Initial setup (face camera towards +Z)
    vrm.scene.rotation.y = Math.PI;
    return vrm;
  }

  flip() {
    if (this.vrm && this.vrm.scene) {
      this.vrm.scene.rotation.y = (this.vrm.scene.rotation.y + Math.PI) % (Math.PI * 2);
    }
  }

  update(delta) {
    if (!this.vrm) return;

    // 1. Reset normalized pose each frame to avoid accumulation distortion
    if (this.vrm.humanoid) {
      this.vrm.humanoid.resetNormalizedPose();
    }

    // 2. Apply natural idle rest pose (arms down, breathing, subtle head motion)
    this.applyIdlePose(delta);

    // 3. Life signs: Blinking
    this.updateBlinking(delta);

    // 4. LookAt smooth interpolation
    this.currentLookPos.lerp(this.targetLookPos, Math.min(1.0, delta * 6.0));
    this.lookTarget.position.copy(this.currentLookPos);

    // 5. Animation mixer
    if (this.mixer) {
      this.mixer.update(delta);
    }

    // 6. VRM Spring bones & constraints
    this.vrm.update(delta);

    // 7. Render
    this.renderer.render(this.scene, this.camera);
  }

  applyIdlePose(delta) {
    if (!this.vrm || !this.vrm.humanoid) return;

    this.breathTime += delta;
    const breathCycle = Math.sin((this.breathTime * Math.PI * 2) / 4.0);
    const breathAngle = breathCycle * 0.015;

    // Spine & chest breathing
    const spine = this.vrm.humanoid.getNormalizedBoneNode('spine');
    const chest = this.vrm.humanoid.getNormalizedBoneNode('chest');
    if (spine) spine.rotation.x = -0.01 + breathAngle * 0.4;
    if (chest) chest.rotation.x = 0.01 + breathAngle * 0.8;

    // Subtle head micro-motion (set directly, NO += accumulator)
    this.headNoiseTime += delta * 0.8;
    const head = this.vrm.humanoid.getNormalizedBoneNode('head');
    const neck = this.vrm.humanoid.getNormalizedBoneNode('neck');
    const microYaw = Math.sin(this.headNoiseTime * 1.3) * 0.025;
    const microPitch = Math.sin(this.headNoiseTime * 0.9 + 1.0) * 0.018;
    const microRoll = Math.sin(this.headNoiseTime * 0.7) * 0.012;

    if (head) {
      head.rotation.set(microPitch, microYaw, microRoll);
    }
    if (neck) {
      neck.rotation.set(microPitch * 0.3, microYaw * 0.3, 0);
    }

    // Natural arms resting alongside the torso (removes T-Pose!)
    const leftUpperArm = this.vrm.humanoid.getNormalizedBoneNode('leftUpperArm');
    const leftLowerArm = this.vrm.humanoid.getNormalizedBoneNode('leftLowerArm');
    const leftHand = this.vrm.humanoid.getNormalizedBoneNode('leftHand');
    const leftShoulder = this.vrm.humanoid.getNormalizedBoneNode('leftShoulder');

    if (leftUpperArm) {
      leftUpperArm.rotation.set(0.08, 0.05, 1.25 - breathAngle * 0.3);
    }
    if (leftLowerArm) {
      leftLowerArm.rotation.set(0.18, 0.0, 0.12);
    }
    if (leftHand) {
      leftHand.rotation.set(0.05, 0.0, 0.05);
    }
    if (leftShoulder) {
      leftShoulder.rotation.set(0.0, 0.0, 0.04);
    }

    const rightUpperArm = this.vrm.humanoid.getNormalizedBoneNode('rightUpperArm');
    const rightLowerArm = this.vrm.humanoid.getNormalizedBoneNode('rightLowerArm');
    const rightHand = this.vrm.humanoid.getNormalizedBoneNode('rightHand');
    const rightShoulder = this.vrm.humanoid.getNormalizedBoneNode('rightShoulder');

    if (rightUpperArm) {
      rightUpperArm.rotation.set(0.08, -0.05, -1.25 + breathAngle * 0.3);
    }
    if (rightLowerArm) {
      rightLowerArm.rotation.set(0.18, 0.0, -0.12);
    }
    if (rightHand) {
      rightHand.rotation.set(0.05, 0.0, -0.05);
    }
    if (rightShoulder) {
      rightShoulder.rotation.set(0.0, 0.0, -0.04);
    }
  }

  updateBlinking(delta) {
    if (!this.vrm || !this.vrm.expressionManager) return;

    this.blinkTimer += delta;
    if (!this.isBlinking && this.blinkTimer >= this.nextBlinkInterval) {
      this.isBlinking = true;
      this.blinkProgress = 0;
      this.blinkTimer = 0;
      this.nextBlinkInterval = 2.0 + Math.random() * 4.0;
    }

    if (this.isBlinking) {
      this.blinkProgress += delta / 0.16; // ~160ms blink duration
      let weight = 0;
      if (this.blinkProgress <= 0.5) {
        weight = this.blinkProgress * 2;
      } else if (this.blinkProgress <= 1.0) {
        weight = (1.0 - this.blinkProgress) * 2;
      } else {
        this.isBlinking = false;
        weight = 0;
      }
      this.vrm.expressionManager.setValue('blink', weight);
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
