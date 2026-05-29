export class RedisBus {
  constructor(config) {
    this.config = config;
    this.client = null;
    this.enabled = Boolean(config.redis.enabled && config.redis.url);
    this.status = this.enabled ? "connecting" : "disabled";
    this.error = "";
  }

  async start() {
    if (!this.enabled || this.client) return;
    try {
      const module = await import("ioredis");
      const Redis = module.default || module.Redis;
      this.client = new Redis(this.config.redis.url, {
        lazyConnect: true,
        maxRetriesPerRequest: 1,
        enableOfflineQueue: false
      });
      this.client.on("error", (error) => {
        this.status = "error";
        this.error = error.message;
      });
      this.client.on("connect", () => {
        this.status = "connected";
        this.error = "";
      });
      await this.client.connect();
    } catch (error) {
      this.status = "unavailable";
      this.error = error.message;
      this.client = null;
    }
  }

  async publish(topic, payload) {
    if (!this.client || this.status === "disabled") return false;
    try {
      await this.client.publish(`${this.config.redis.namespace}:${topic}`, JSON.stringify(payload));
      return true;
    } catch (error) {
      this.status = "error";
      this.error = error.message;
      return false;
    }
  }

  snapshot() {
    return {
      enabled: this.enabled,
      status: this.status,
      namespace: this.config.redis.namespace,
      error: this.error
    };
  }
}
