import { test, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { activateBrowserAuthScope, createSession, loadSessions, saveSessions } from '../src/lib/store.ts';

class MemoryStorage {
  rows = new Map<string, string>();
  fail = false;
  getItem(key: string) { return this.rows.get(key) ?? null; }
  setItem(key: string, value: string) { if (this.fail) throw new Error('quota'); this.rows.set(key, value); }
  removeItem(key: string) { this.rows.delete(key); }
}

const storage = new MemoryStorage();
Object.defineProperty(globalThis, 'localStorage', { value: storage, configurable: true });
Object.defineProperty(globalThis, 'window', { value: new EventTarget(), configurable: true });
beforeEach(() => { storage.rows.clear(); storage.fail = false; activateBrowserAuthScope('local'); });

test('连续新建的空会话在刷新读取后都保留', () => {
  const previous = { ...createSession(), id: 'existing', title: '旧会话', messages: [{ role: 'user' as const, content: '旧消息' }] };
  const added = [createSession(), createSession(), createSession()];
  assert.equal(new Set(added.map((item) => item.id)).size, 3);
  assert.equal(saveSessions([...added, previous]), true);
  const loaded = loadSessions();
  assert.deepEqual(loaded.map((item) => item.id), [...added.map((item) => item.id), 'existing']);
  assert.equal(loaded[3].messages[0].content, '旧消息');
});

test('旧会话保持 ID 和消息，补全排序时间', () => {
  storage.setItem('ripple_sessions', JSON.stringify([{ id: 'old-id', title: 'Old Chat', created: 123, messages: [{ role: 'assistant', content: '原消息' }], archived: true }]));
  const [item] = loadSessions();
  assert.equal(item.id, 'old-id');
  assert.equal(item.updatedAt, 123);
  assert.equal(item.archived, true);
  assert.equal(item.messages[0].content, '原消息');
});

test('存储空间不足时保留原记录并发出错误通知', () => {
  const initial = [createSession()];
  assert.equal(saveSessions(initial), true);
  const before = storage.getItem('ripple_sessions');
  let seen = 0;
  const onError = () => { seen += 1; };
  window.addEventListener('ripple:session-storage-error', onError);
  storage.fail = true;
  assert.equal(saveSessions([createSession(), ...initial]), false);
  assert.equal(storage.getItem('ripple_sessions'), before);
  assert.equal(seen, 1);
  window.removeEventListener('ripple:session-storage-error', onError);
});
