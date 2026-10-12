/**
 * WebSocket client with auto-reconnection and protocol handlers.
 */

export class VTuberWSClient {
  constructor(url = 'ws://127.0.0.1:8765/ws') {
    this.url = url;
    this.ws = null;
    this.isConnected = false;
    this.reconnectDelay = 1000;
    this.maxReconnectDelay = 5000;
    this.handlers = new Map();

    this.connect();
  }

  on(type, callback) {
    if (!this.handlers.has(type)) {
      this.handlers.set(type, []);
    }
    this.handlers.get(type).push(callback);
  }

  emit(type, data) {
    const list = this.handlers.get(type);
    if (list) {
      for (const cb of list) {
        cb(data);
      }
    }
  }

  connect() {
    try {
      this.ws = new WebSocket(this.url);

      this.ws.onopen = () => {
        this.isConnected = true;
        this.reconnectDelay = 1000;
        console.log('[WS] Connected to daemon at', this.url);
        this.send({ type: 'ready' });
        this.emit('connected');
      };

      this.ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data);
          if (msg && msg.type) {
            this.emit(msg.type, msg);
          }
        } catch (err) {
          console.error('[WS] Failed to parse message:', err);
        }
      };

      this.ws.onclose = () => {
        this.isConnected = false;
        this.emit('disconnected');
        this.scheduleReconnect();
      };

      this.ws.onerror = (err) => {
        console.warn('[WS] Socket error:', err);
        if (this.ws) {
          this.ws.close();
        }
      };
    } catch (err) {
      console.error('[WS] Connection error:', err);
      this.scheduleReconnect();
    }
  }

  scheduleReconnect() {
    setTimeout(() => {
      this.reconnectDelay = Math.min(this.maxReconnectDelay, this.reconnectDelay * 1.5);
      this.connect();
    }, this.reconnectDelay);
  }

  send(data) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(data));
    }
  }

  sendPlaybackStarted(id) {
    this.send({ type: 'playback_started', id });
  }

  sendPlaybackEnded(id) {
    this.send({ type: 'playback_ended', id });
  }

  sendClick(region) {
    this.send({ type: 'click', region });
  }
}
