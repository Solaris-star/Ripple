/** Short-lived, view-keyed list cache. Never persists account activity data to disk. */
export class CampaignPageCache<T> {
  private readonly entries = new Map<string, { value: T; expiresAt: number }>();
  private readonly ttlMs: number;
  private readonly maxEntries: number;
  private revision = 0;

  constructor(ttlMs = 60_000, maxEntries = 32) {
    this.ttlMs = ttlMs;
    this.maxEntries = maxEntries;
  }

  get generation(): number { return this.revision; }
  get size(): number { return this.entries.size; }

  get(key: string, now = Date.now()): T | undefined {
    const entry = this.entries.get(key);
    if (!entry) return undefined;
    if (entry.expiresAt <= now) {
      this.entries.delete(key);
      return undefined;
    }
    return entry.value;
  }

  set(key: string, value: T, generation = this.revision, now = Date.now()): boolean {
    // A pre-mutation response must not repopulate an invalidated cache.
    if (generation !== this.revision) return false;
    this.entries.delete(key);
    this.entries.set(key, { value, expiresAt: now + this.ttlMs });
    while (this.entries.size > this.maxEntries) {
      const oldest = this.entries.keys().next().value;
      if (oldest === undefined) break;
      this.entries.delete(oldest);
    }
    return true;
  }

  clear(): void {
    this.revision += 1;
    this.entries.clear();
  }
}
