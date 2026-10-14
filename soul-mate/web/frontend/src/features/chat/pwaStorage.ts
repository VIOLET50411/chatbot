export interface PwaStoredMessage {
  id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  createdAt: string;
  delivery?: 'sending' | 'sent' | 'failed';
  error?: string;
}

const DATABASE_NAME = 'soul-mate-pwa';
const DATABASE_VERSION = 1;
const MESSAGE_STORE = 'messages';

function openDatabase(): Promise<IDBDatabase | null> {
  if (typeof indexedDB === 'undefined') return Promise.resolve(null);
  return new Promise((resolve) => {
    const request = indexedDB.open(DATABASE_NAME, DATABASE_VERSION);
    request.onupgradeneeded = () => {
      const database = request.result;
      if (!database.objectStoreNames.contains(MESSAGE_STORE)) {
        database.createObjectStore(MESSAGE_STORE, { keyPath: 'key' });
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => resolve(null);
  });
}

export async function loadCachedMessages(key: string): Promise<PwaStoredMessage[]> {
  const database = await openDatabase();
  if (!database) return [];
  return new Promise((resolve) => {
    const transaction = database.transaction(MESSAGE_STORE, 'readonly');
    const request = transaction.objectStore(MESSAGE_STORE).get(key);
    request.onsuccess = () => {
      const value = request.result?.messages;
      resolve(Array.isArray(value) ? value : []);
    };
    request.onerror = () => resolve([]);
    transaction.oncomplete = () => database.close();
    transaction.onerror = () => database.close();
  });
}

export async function saveCachedMessages(key: string, messages: PwaStoredMessage[]): Promise<void> {
  const database = await openDatabase();
  if (!database) return;
  await new Promise<void>((resolve) => {
    const transaction = database.transaction(MESSAGE_STORE, 'readwrite');
    transaction.objectStore(MESSAGE_STORE).put({
      key,
      messages: messages.slice(-300),
      savedAt: new Date().toISOString(),
    });
    transaction.oncomplete = () => resolve();
    transaction.onerror = () => resolve();
    transaction.onabort = () => resolve();
  });
  database.close();
}
