import * as THREE from 'three';
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';
import { BVHLoader } from 'three/examples/jsm/loaders/BVHLoader.js';
import { VRMAnimationLoaderPlugin, createVRMAnimationClip } from '@pixiv/three-vrm-animation';

/**
 * Universal VRM Animation Loader.
 * Supports native .vrma files and retargeted mocap .bvh files with Humanoid bone mapping.
 */
export class AnimationLoader {
  constructor(vrm = null) {
    this.vrm = vrm;
    this.cache = new Map();

    this.gltfLoader = new GLTFLoader();
    this.gltfLoader.register((parser) => new VRMAnimationLoaderPlugin(parser));

    this.bvhLoader = new BVHLoader();
  }

  setVRM(vrm) {
    this.vrm = vrm;
    this.cache.clear();
  }

  /**
   * Load animation clip from URL (.vrma or .bvh).
   * @param {string} url
   * @returns {Promise<THREE.AnimationClip|null>}
   */
  async loadAnimation(url) {
    if (!this.vrm) return null;

    if (this.cache.has(url)) {
      return this.cache.get(url);
    }

    try {
      let clip = null;
      const lowerUrl = url.toLowerCase();

      if (lowerUrl.endsWith('.vrma')) {
        clip = await this.loadVRMA(url);
      } else if (lowerUrl.endsWith('.bvh')) {
        clip = await this.loadBVH(url);
      } else {
        console.warn('[AnimationLoader] Unsupported animation format:', url);
        return null;
      }

      if (clip) {
        this.cache.set(url, clip);
      }
      return clip;
    } catch (err) {
      console.error(`[AnimationLoader] Error loading ${url}:`, err);
      return null;
    }
  }

  /**
   * Load .vrma using @pixiv/three-vrm-animation.
   */
  async loadVRMA(url) {
    const gltf = await this.gltfLoader.loadAsync(url);
    const vrmAnimation = gltf.userData.vrmAnimations?.[0] || gltf.userData.vrmAnimation;
    if (!vrmAnimation) {
      throw new Error(`Missing vrmAnimation metadata in ${url}`);
    }
    return createVRMAnimationClip(vrmAnimation, this.vrm);
  }

  /**
   * Load .bvh and retarget joints to VRM Humanoid NormalizedBones.
   */
  async loadBVH(url) {
    const result = await this.bvhLoader.loadAsync(url);
    const clip = result.clip;
    const tracks = [];

    const restRotationInverse = new THREE.Quaternion();
    const parentRestWorldRotation = new THREE.Quaternion();
    const _quatA = new THREE.Quaternion();

    // Scale hips motion relative to VRM model height
    const motionHips = result.skeleton.getBoneByName('hips');
    const motionHipsHeight = motionHips ? Math.abs(motionHips.position.y) : 10.0;
    const vrmHipsNode = this.vrm.humanoid?.getNormalizedBoneNode('hips');
    const vrmHipsHeight = vrmHipsNode ? Math.abs(vrmHipsNode.position.y) : 0.8;
    const hipsPositionScale = motionHipsHeight > 0.001 ? vrmHipsHeight / motionHipsHeight : 1.0;

    const isVRM0 = this.vrm.meta?.metaVersion === '0';

    for (const track of clip.tracks) {
      const parts = track.name.split('.');
      const vrmBoneName = parts[0];
      const propertyName = parts[1];

      const vrmNode = this.vrm.humanoid?.getNormalizedBoneNode(vrmBoneName);
      if (!vrmNode) continue;

      const vrmNodeName = vrmNode.name;
      const bvhRigNode = result.skeleton.getBoneByName(vrmBoneName);
      if (!bvhRigNode) continue;

      if (track instanceof THREE.QuaternionKeyframeTrack) {
        bvhRigNode.getWorldQuaternion(restRotationInverse).invert();
        if (bvhRigNode.parent) {
          bvhRigNode.parent.getWorldQuaternion(parentRestWorldRotation);
        } else {
          parentRestWorldRotation.identity();
        }

        const values = new Float32Array(track.values.length);
        for (let i = 0; i < track.values.length; i += 4) {
          _quatA.fromArray(track.values, i);
          _quatA.premultiply(parentRestWorldRotation).multiply(restRotationInverse);
          _quatA.toArray(values, i);
        }

        // Handle VRM 0.0 coordinate inversion
        if (isVRM0) {
          for (let i = 0; i < values.length; i += 4) {
            values[i] = -values[i];
            values[i + 2] = -values[i + 2];
          }
        }

        tracks.push(
          new THREE.QuaternionKeyframeTrack(
            `${vrmNodeName}.${propertyName}`,
            track.times,
            values
          )
        );
      } else if (track instanceof THREE.VectorKeyframeTrack && vrmBoneName === 'hips') {
        const values = new Float32Array(track.values.length);
        for (let i = 0; i < track.values.length; i += 3) {
          const x = track.values[i] * hipsPositionScale;
          const y = track.values[i + 1] * hipsPositionScale;
          const z = track.values[i + 2] * hipsPositionScale;
          values[i] = isVRM0 ? -x : x;
          values[i + 1] = y;
          values[i + 2] = isVRM0 ? -z : z;
        }

        tracks.push(
          new THREE.VectorKeyframeTrack(
            `${vrmNodeName}.${propertyName}`,
            track.times,
            values
          )
        );
      }
    }

    return new THREE.AnimationClip(url, clip.duration, tracks);
  }
}
