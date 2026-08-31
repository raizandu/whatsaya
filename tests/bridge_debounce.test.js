import test from 'node:test';
import assert from 'node:assert';
import fs from 'node:fs';
import path from 'node:path';

const TEST_ROOT = path.join('/tmp', `whatsaya-bridge-debounce-test-${process.pid}`);
fs.mkdirSync(TEST_ROOT, { recursive: true });
process.on('exit', () => fs.rmSync(TEST_ROOT, { recursive: true, force: true }));
process.env.HOME = TEST_ROOT;
process.env.WHATSAPP_HISTORY_DB_PATH = path.join(TEST_ROOT, 'whatsapp_messages.db');
process.env.WHATSAPP_HISTORY_PERSIST_DISABLED = 'true';
process.env.WHATSAPP_OWNER_NUMBER = '99999';
process.env.WHATSAPP_ALLOWED_USERS = 'client123';
process.env.WHATSAPP_MODE = 'bot';
process.env.WHATSAPP_DEBOUNCE_INITIAL_MS = '120';
process.env.WHATSAPP_DEBOUNCE_MIN_MS = '30';
process.env.WHATSAPP_DEBOUNCE_DECAY = '0.5';
process.env.WHATSAPP_DEBOUNCE_SKIP_SELF_CHAT = 'true';
process.env.WHATSAPP_DEBOUNCE_TYPING_REFRESH_MS = '25';
process.env.WHATSAPP_SEND_READ_RECEIPTS = 'true';

const {
  clearRecentlyProcessedIds,
  getMessageQueue,
  onMessagesUpsert,
  setSock,
} = await import('../bridge.js');

const presenceUpdates = [];
const readReceiptKeys = [];
let blockReadReceipt = false;
let releaseReadReceipt = null;
setSock({
  sendPresenceUpdate: async (state, chatId) => {
    presenceUpdates.push({ state, chatId, at: Date.now() });
  },
  readMessages: async (keys) => {
    readReceiptKeys.push(...keys);
    if (blockReadReceipt) {
      await new Promise((resolve) => { releaseReadReceipt = resolve; });
    }
  },
});

const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
const inbound = (id, body) => ({
  messages: [{
    key: {
      id,
      fromMe: false,
      remoteJid: 'client123@s.whatsapp.net',
    },
    message: { conversation: body },
  }],
  type: 'notify',
});

test('quick text fragments become one ordered inbound batch', async () => {
  clearRecentlyProcessedIds();
  getMessageQueue().length = 0;
  presenceUpdates.length = 0;
  readReceiptKeys.length = 0;

  blockReadReceipt = true;
  const processing = onMessagesUpsert(inbound('debounce-1', 'Sim, pode ser'));
  const processingOutcome = await Promise.race([
    processing.then(() => 'completed'),
    wait(50).then(() => 'blocked'),
  ]);
  releaseReadReceipt?.();
  blockReadReceipt = false;
  await processing;

  assert.strictEqual(
    processingOutcome,
    'completed',
    'a slow read-receipt acknowledgement must not delay composing or start the debounce late',
  );
  assert.deepStrictEqual(readReceiptKeys.map(key => key.id), ['debounce-1']);
  assert.strictEqual(presenceUpdates[0]?.state, 'composing');
  assert.strictEqual(presenceUpdates[0]?.chatId, 'client123@s.whatsapp.net');

  await wait(35);
  assert.ok(
    presenceUpdates.length >= 2,
    'typing presence should be refreshed while the first fragment remains buffered',
  );

  await wait(25);
  await onMessagesUpsert(inbound('debounce-2', 'Quanto q custa?'));
  assert.deepStrictEqual(
    readReceiptKeys.map(key => key.id),
    ['debounce-1', 'debounce-2'],
    'each inbound fragment should be marked read before the debounce flush',
  );

  assert.strictEqual(getMessageQueue().length, 0);
  await wait(100);

  const queue = getMessageQueue();
  assert.strictEqual(queue.length, 1);
  assert.strictEqual(queue[0].body, 'Sim, pode ser\nQuanto q custa?');
  assert.deepStrictEqual(queue[0].debounceIds, ['debounce-1', 'debounce-2']);

  const updatesAtFlush = presenceUpdates.length;
  await wait(40);
  assert.strictEqual(
    presenceUpdates.length,
    updatesAtFlush,
    'typing refresh should stop once the buffered turn is flushed',
  );
});
