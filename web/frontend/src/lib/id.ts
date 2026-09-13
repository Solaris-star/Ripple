function randomHex(bytes: number): string {
  const values = new Uint8Array(bytes);
  if (globalThis.crypto?.getRandomValues) {
    globalThis.crypto.getRandomValues(values);
  } else {
    for (let index = 0; index < values.length; index += 1) {
      values[index] = Math.floor(Math.random() * 256);
    }
  }
  return Array.from(values, (value) => value.toString(16).padStart(2, '0')).join('');
}

/**
 * Return a UUID without assuming a secure browser context.
 *
 * crypto.randomUUID() is unavailable when Ripple is opened over plain HTTP from
 * a LAN address. getRandomValues() remains available in supported browsers; the
 * final branch only keeps local development usable on older WebViews.
 */
export function newId(): string {
  if (typeof globalThis.crypto?.randomUUID === 'function') {
    return globalThis.crypto.randomUUID();
  }
  const hex = randomHex(16).split('');
  hex[12] = '4';
  const variant = Number.parseInt(hex[16], 16);
  hex[16] = ((variant & 0x3) | 0x8).toString(16);
  return `${hex.slice(0, 8).join('')}-${hex.slice(8, 12).join('')}-${hex.slice(12, 16).join('')}-${hex.slice(16, 20).join('')}-${hex.slice(20).join('')}`;
}
