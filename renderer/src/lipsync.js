/**
 * High-fidelity Formant-based Lipsync & Audio Manager.
 * Analyzes frequency spectra to morph between Japanese/VRM anime vowels (A, I, U, E, O).
 */

export class LipsyncManager {
  constructor(vrm) {
    this.vrm = vrm;
    this.audioContext = null;
    this.analyser = null;
    this.timeData = null;
    this.freqData = null;
    this.currentSource = null;
    this.isPlaying = false;
    this.currentUtteranceId = null;

    // Smoothed viseme weights
    this.visemes = {
      aa: 0.0,
      ih: 0.0,
      ou: 0.0,
      ee: 0.0,
      oh: 0.0,
    };
    this.currentVolume = 0.0;

    this.queue = [];
    this.muteOutput = false;

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
    this.analyser.smoothingTimeConstant = 0.25; // Snappy response to phonemes
    this.timeData = new Float32Array(this.analyser.fftSize);
    this.freqData = new Uint8Array(this.analyser.frequencyBinCount);
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
      this.currentVolume = 0.0;
      return;
    }

    this.isPlaying = true;
    const item = this.queue.shift();
    this.currentUtteranceId = item.id;

    try {
      this.ensureContext();
      if (this.audioContext && this.audioContext.state === 'suspended') {
        await this.audioContext.resume();
      }

      const audioUrl = item.audioUrl.startsWith('http')
        ? item.audioUrl
        : `http://127.0.0.1:8765${item.audioUrl}`;

      const response = await fetch(audioUrl);
      if (!response.ok) {
        throw new Error(`Failed to fetch audio from ${audioUrl}: ${response.statusText}`);
      }
      const arrayBuffer = await response.arrayBuffer();
      const audioBuffer = await this.audioContext.decodeAudioData(arrayBuffer);

      if (!this.isPlaying || this.currentUtteranceId !== item.id) {
        return;
      }

      const source = this.audioContext.createBufferSource();
      source.buffer = audioBuffer;
      source.connect(this.analyser);
      if (!this.muteOutput) {
        this.analyser.connect(this.audioContext.destination);
      }

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
    this.currentVolume = 0.0;

    for (const k of Object.keys(this.visemes)) {
      this.visemes[k] = 0.0;
    }

    if (this.vrm && this.vrm.expressionManager) {
      for (const v of ['aa', 'ih', 'ou', 'ee', 'oh']) {
        try {
          this.vrm.expressionManager.setValue(v, 0.0);
        } catch {}
      }
    }

    if (stoppedId && this.onPlaybackEnded) {
      this.onPlaybackEnded(stoppedId);
    }
  }

  update(delta) {
    if (!this.vrm || !this.vrm.expressionManager || !this.analyser) return;

    if (!this.isPlaying || !this.currentSource) {
      // Softly decay mouth towards closed rest pose
      let anyOpen = false;
      for (const [k, v] of Object.entries(this.visemes)) {
        if (v > 0.005) {
          this.visemes[k] = Math.max(0.0, v - delta * 12.0);
          try {
            this.vrm.expressionManager.setValue(k, this.visemes[k]);
          } catch {}
          anyOpen = true;
        } else if (v > 0.0) {
          this.visemes[k] = 0.0;
          try {
            this.vrm.expressionManager.setValue(k, 0.0);
          } catch {}
        }
      }
      this.currentVolume = Math.max(0.0, this.currentVolume - delta * 10.0);
      return;
    }

    // 1. Analyze time-domain energy (RMS)
    this.analyser.getFloatTimeDomainData(this.timeData);
    let sumSquares = 0;
    for (let i = 0; i < this.timeData.length; i++) {
      sumSquares += this.timeData[i] * this.timeData[i];
    }
    const rms = Math.sqrt(sumSquares / this.timeData.length);
    const volume = Math.min(1.0, Math.max(0.0, (rms - 0.015) * 12.0));
    this.currentVolume = volume;

    // 2. Frequency formant decomposition
    this.analyser.getByteFrequencyData(this.freqData);

    const getBandEnergy = (startBin, endBin) => {
      let sum = 0;
      for (let i = startBin; i <= endBin && i < this.freqData.length; i++) {
        sum += this.freqData[i];
      }
      return sum / (endBin - startBin + 1) / 255.0;
    };

    // Low formant ~ 150 - 650 Hz (ou / oh)
    const lowEnergy = getBandEnergy(3, 14);
    // Mid formant ~ 650 - 1800 Hz (aa)
    const midEnergy = getBandEnergy(15, 38);
    // High formant ~ 1800 - 3800 Hz (ee / ih)
    const highEnergy = getBandEnergy(39, 82);

    const totalFormant = lowEnergy + midEnergy + highEnergy + 0.0001;

    // Raw targets weighted by overall amplitude
    let targetAa = (midEnergy / totalFormant) * volume * 1.1;
    let targetIh = (highEnergy / totalFormant) * volume * 0.7;
    let targetOu = (lowEnergy / totalFormant) * volume * 0.8;
    let targetEe = ((highEnergy * 0.6 + midEnergy * 0.4) / totalFormant) * volume * 0.65;
    let targetOh = ((lowEnergy * 0.7 + midEnergy * 0.3) / totalFormant) * volume * 0.75;

    // Fast attack (open mouth instantly on sound), smooth release (natural damping)
    const attackSpeed = 24.0;
    const releaseSpeed = 14.0;

    const smooth = (curr, target) => {
      const speed = target > curr ? attackSpeed : releaseSpeed;
      return curr + (target - curr) * Math.min(1.0, delta * speed);
    };

    this.visemes.aa = Math.min(0.85, smooth(this.visemes.aa, targetAa));
    this.visemes.ih = Math.min(0.65, smooth(this.visemes.ih, targetIh));
    this.visemes.ou = Math.min(0.70, smooth(this.visemes.ou, targetOu));
    this.visemes.ee = Math.min(0.60, smooth(this.visemes.ee, targetEe));
    this.visemes.oh = Math.min(0.70, smooth(this.visemes.oh, targetOh));

    // Apply to VRM expression manager (supports VRM 1.0 presets & VRM 0.0 auto-mapped aliases)
    try {
      this.vrm.expressionManager.setValue('aa', this.visemes.aa);
      this.vrm.expressionManager.setValue('ih', this.visemes.ih);
      this.vrm.expressionManager.setValue('ou', this.visemes.ou);
      this.vrm.expressionManager.setValue('ee', this.visemes.ee);
      this.vrm.expressionManager.setValue('oh', this.visemes.oh);
    } catch (err) {
      // Fallback
    }
  }
}
