/**
 * Lipsync and audio queue manager using Web Audio API.
 */

export class LipsyncManager {
  constructor(vrm) {
    this.vrm = vrm;
    this.audioContext = null;
    this.analyser = null;
    this.dataBuffer = null;
    this.currentSource = null;
    this.mouthOpen = 0.0;
    this.isPlaying = false;
    this.currentUtteranceId = null;

    this.queue = [];

    // Callbacks
    this.onPlaybackStarted = null;
    this.onPlaybackEnded = null;

    this.initAudio();
  }

  setVRM(vrm) {
    this.vrm = vrm;
  }

  initAudio() {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass) return;

    this.audioContext = new AudioContextClass();
    this.analyser = this.audioContext.createAnalyser();
    this.analyser.fftSize = 1024;
    this.analyser.smoothingTimeConstant = 0.4;
    this.dataBuffer = new Float32Array(this.analyser.fftSize);
  }

  ensureContext() {
    if (this.audioContext && this.audioContext.state === 'suspended') {
      this.audioContext.resume();
    }
  }

  enqueue(item) {
    this.ensureContext();
    this.queue.push(item);
    if (!this.isPlaying) {
      this.playNext();
    }
  }

  async playNext() {
    if (this.queue.length === 0) {
      this.isPlaying = false;
      this.currentUtteranceId = null;
      return;
    }

    this.isPlaying = true;
    const item = this.queue.shift();
    this.currentUtteranceId = item.id;

    try {
      const response = await fetch(item.audioUrl);
      if (!response.ok) {
        throw new Error(`Failed to fetch audio: ${response.statusText}`);
      }
      const arrayBuffer = await response.arrayBuffer();
      const audioBuffer = await this.audioContext.decodeAudioData(arrayBuffer);

      if (!this.isPlaying || this.currentUtteranceId !== item.id) {
        return; // Stopped while decoding
      }

      const source = this.audioContext.createBufferSource();
      source.buffer = audioBuffer;
      source.connect(this.analyser);
      this.analyser.connect(this.audioContext.destination);

      this.currentSource = source;

      if (this.onPlaybackStarted) {
        this.onPlaybackStarted(item.id, item);
      }

      source.onended = () => {
        if (this.currentSource === source) {
          this.currentSource = null;
        }
        if (this.onPlaybackEnded) {
          this.onPlaybackEnded(item.id);
        }
        this.playNext();
      };

      source.start(0);
    } catch (err) {
      console.error('Audio playback error:', err);
      if (this.onPlaybackEnded && item.id) {
        this.onPlaybackEnded(item.id);
      }
      this.playNext();
    }
  }

  stop() {
    this.queue = [];
    if (this.currentSource) {
      try {
        this.currentSource.stop();
      } catch {
        // already stopped
      }
      this.currentSource = null;
    }
    const stoppedId = this.currentUtteranceId;
    this.isPlaying = false;
    this.currentUtteranceId = null;
    this.mouthOpen = 0.0;

    if (this.vrm && this.vrm.expressionManager) {
      this.vrm.expressionManager.setValue('aa', 0.0);
    }

    if (stoppedId && this.onPlaybackEnded) {
      this.onPlaybackEnded(stoppedId);
    }
  }

  update(delta) {
    if (!this.vrm || !this.vrm.expressionManager || !this.analyser) return;

    if (!this.isPlaying || !this.currentSource) {
      // Fade out mouth when not playing
      if (this.mouthOpen > 0.001) {
        this.mouthOpen += (0.0 - this.mouthOpen) * Math.min(1.0, delta * 20.0);
        this.vrm.expressionManager.setValue('aa', this.mouthOpen * 0.7);
      }
      return;
    }

    this.analyser.getFloatTimeDomainData(this.dataBuffer);
    let sum = 0;
    for (let i = 0; i < this.dataBuffer.length; i++) {
      sum += this.dataBuffer[i] * this.dataBuffer[i];
    }
    const rms = Math.sqrt(sum / this.dataBuffer.length);

    // Dynamic threshold calculation
    const target = Math.min(1.0, Math.max(0.0, (rms - 0.02) * 14.0));
    this.mouthOpen += (target - this.mouthOpen) * Math.min(1.0, delta * 18.0);

    // Apply to 'aa' expression with 0.7 scaling to avoid over-distortion
    this.vrm.expressionManager.setValue('aa', this.mouthOpen * 0.7);
  }
}
