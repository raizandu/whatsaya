import test, { after, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';

const testHome = mkdtempSync(join(tmpdir(), 'whatsaya-canonical-history-'));
const dbPath = join(testHome, 'whatsapp_messages.db');
process.argv.push('--session', join(testHome, 'session'));
process.env.WHATSAPP_HISTORY_DB_PATH = dbPath;
process.env.WHATSAPP_HISTORY_PERSIST_DISABLED = 'false';
process.env.WHATSAPP_OWNER_NUMBER = '19999999999';
process.env.WHATSAPP_ALLOWED_USERS = '15557654321';
process.env.WHATSAPP_MODE = 'bot';
process.env.WHATSAPP_DEBOUNCE_INITIAL_MS = '0';

const bridge = await import('../bridge.js');
const { initHistoryStore } = await import('../history_bridge.js');

bridge.setSock({
  user: { id: '19999999999:1@s.whatsapp.net', lid: '111:1@lid' },
  contacts: {},
  readMessages: async () => {},
});

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function storedRows(messageId) {
  const output = execFileSync('python3', [
    '-c',
    [
      'import json, sqlite3, sys',
      'with sqlite3.connect(sys.argv[1]) as conn:',
      " rows = conn.execute('SELECT chat_id, sender_id, message_id FROM messages WHERE message_id=? ORDER BY chat_id', (sys.argv[2],)).fetchall()",
      'print(json.dumps(rows))',
    ].join('\n'),
    dbPath,
    messageId,
  ], { encoding: 'utf8' });
  return JSON.parse(output);
}

beforeEach(async () => {
  bridge.clearRecentlyProcessedIds();
  bridge.getMessageQueue().length = 0;
  await initHistoryStore();
});

after(() => {
  rmSync(testHome, { recursive: true, force: true });
});

test('a live LID message is stored once under its canonical phone identity', async () => {
  const messageId = 'canonical-history-1';
  await bridge.onMessagesUpsert({
    type: 'notify',
    messages: [{
      key: {
        id: messageId,
        fromMe: false,
        remoteJid: '222222222@lid',
        remoteJidAlt: '15557654321@s.whatsapp.net',
      },
      pushName: 'Tony',
      messageTimestamp: 1787490000,
      message: { conversation: 'Oi, quero entender como funciona' },
    }],
  });

  let rows = [];
  for (let attempt = 0; attempt < 20; attempt += 1) {
    await wait(50);
    rows = storedRows(messageId);
    if (rows.length >= 1) break;
  }
  await wait(250);
  rows = storedRows(messageId);

  assert.deepEqual(rows, [[
    '15557654321@s.whatsapp.net',
    '15557654321@s.whatsapp.net',
    messageId,
  ]]);
});
