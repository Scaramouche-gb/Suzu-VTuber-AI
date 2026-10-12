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
    this.targetLookPos = new THREE.Vector3(0, 1.4, 2.0);
    this.currentLookPos = new THREE.Vector3(0, 1.4, 2.0);

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

    const ambientLight = new THREE.AmbientLight(0xffffff, 0.65);
    this.scene.add(ambientLight);

    this.lookTarget = new THREE.Object3D();
    this.lookTarget.position.copy(this.currentLookPos);
    this.scene.add(this.lookTarget);

    this.onWindowResize();
  }

  updateCameraPlacement() {
    if (this.fitMode === 'bust') {
      // Focus on head and chest
      this.camera.position.set(0, 1.35 + this.yOffset, 1.25 / this.scale);
      this.camera.lookAt(0, 1.30 + this.yOffset, 0);
    } else {
      // Full body
      this.camera.position.set(0, 1.0 + this.yOffset, 3.2 / this.scale);
      this.camera.lookAt(0, 0.9 + this.yOffset, 0);
    }
  }

  initEvents() {
    window.addEventListener('resize', () => this.onWindowResize());
    window.addEventListener('mousemove', (e) => {
      // Normalize to -1 .. 1
      this.cursorPosition.x = (e.clientX / window.innerWidth) * 2 - 1;
      this.cursorPosition.y = -(e.clientY / window.innerHeight) * 2 + 1;

      // Project onto look target
      this.targetLookPos.set(
        this.cursorPosition.x * 0.8,
        1.35 + this.cursorPosition.y * 0.4 + this.yOffset,
        1.5
      );
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
    VRMUtils.rotateVRM0(vrm);

    if (this.vrm) {
      this.scene.remove(this.vrm.scene);
    }

    this.vrm = vrm;
    this.scene.add(vrm.scene);

    if (vrm.lookAt) {
      vrm.lookAt.target = this.lookTarget;
    }

    this.mixer = new THREE.AnimationMixer(vrm.scene);

    // Initial setup
    vrm.scene.rotation.y = 0;
    return vrm;
  }

  update(delta) {
    if (!this.vrm) return;

    // 1. Life signs: Blinking
    this.updateBlinking(delta);

    // 2. Life signs: Breathing
    this.updateBreathing(delta);

    // 3. Life signs: Head micro-motion
    this.updateHeadMicroMotion(delta);

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

  updateBreathing(delta) {
    if (!this.vrm || !this.vrm.humanoid) return;

    this.breathTime += delta;
    const period = 4.0; // 4 seconds
    const angle = Math.sin((this.breathTime * Math.PI * 2) / period) * 0.015;

    const chest = this.vrm.humanoid.getNormalizedBoneNode('chest');
    const spine = this.vrm.humanoid.getNormalizedBoneNode('spine');

    if (chest) {
      chest.rotation.x = angle;
    }
    if (spine) {
      spine.rotation.x = angle * 0.5;
    }
  }

  updateHeadMicroMotion(delta) {
    if (!this.vrm || !this.vrm.humanoid) return;

    this.headNoiseTime += delta * 0.8;
    const head = this.vrm.humanoid.getNormalizedBoneNode('head');
    if (!head) return;

    const microYaw = Math.sin(this.headNoiseTime * 1.3) * 0.02;
    const microPitch = Math.cos(this.headNoiseTime * 0.9) * 0.015;
    const microRoll = Math.sin(this.headNoiseTime * 0.7) * 0.01;

    head.rotation.y = (head.rotation.y || 0) + microYaw * delta;
    head.rotation.x = (head.rotation.x || 0) + microPitch * delta;
    head.rotation.z = (head.rotation.z || 0) + microRoll * delta;
  }

  setFit(mode, scale = 1.0, yOffset = 0.0) {
    this.fitMode = mode;
    this.scale = scale;
    this.yOffset = yOffset;
    this.updateCameraPlacement();
  }

  setLookTarget(mode, x = 0.5, y = 0.3) {
    if (mode === 'screen') {
      // Look slightly down and towards screen center
      this.targetLookPos.set(0.1, 1.15 + this.yOffset, 0.9);
    } else if (mode === 'user') {
      // Look directly into camera
      this.targetLookPos.set(0, 1.35 + this.yOffset, 2.0);
    } else if (mode === 'point') {
      this.targetLookPos.set((x - 0.5) * 1.2, 1.35 + (0.5 - y) * 0.8 + this.yOffset, 1.5);
    }
  }
}
