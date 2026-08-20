import { spawn } from 'child_process';
import path from 'path';
import { fileURLToPath } from 'url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const STORE_SCRIPT = path.join(HERE, 'history_store.py');
const HERMES_HOME = process.env.HERMES_HOME || '/opt/data/.hermes';
const DB_PATH = process.env.WHATSAPP_HISTORY_DB_PATH || path.join(HERMES_HOME, 'whatsapp_messages.db');

const historyStats = {
  initialized: false,
  batches: 0,
  received: 0,
  inserted: 0,
  skipped: 0,
  lastSyncType: null,
  lastBatchAt: null,
  lastError: null,
};

function runStore(command, args = [], payload = null) {
  return new Promise((resolve, reject) => {
    const child = spawn('python3', [STORE_SCRIPT, command, DB_PATH, ...args], {
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (chunk) => { stdout += chunk.toString(); });
    child.stderr.on('data', (chunk) => { stderr += chunk.toString(); });
    child.on('error', reject);
    child.on('close', (code) => {
      if (code !== 0) {
        reject(new Error(stderr.trim() || `history_store exit=${code}`));
        return;
      }
      try {
        resolve(stdout.trim() ? JSON.parse(stdout) : { ok: true });
      } catch (err) {
        reject(new Error(`resposta inválida do history_store: ${err.message}`));
      }
    });
    if (payload == null) child.stdin.end();
    else child.stdin.end(JSON.stringify(payload));
  });
}

function unwrapMessageContent(message) {
  let content = message || {};
  const wrappers = [
    'ephemeralMessage',
    'viewOnceMessage',
    'viewOnceMessageV2',
    'documentWithCaptionMessage',
  ];
  for (let i = 0; i < 4; i += 1) {
    let changed = false;
    for (const wrapper of wrappers) {
      if (content?.[wrapper]?.message) {
        content = content[wrapper].message;
        changed = true;
        break;
      }
    }
    if (!changed) break;
  }
  return content || {};
}

function messageTimestamp(message) {
  const value = message?.messageTimestamp;
  try {
    if (value && typeof value.toNumber === 'function') return value.toNumber();
    if (value && typeof value.low === 'number') return value.low;
    const parsed = Number(value);
    if (Number.isFinite(parsed) && parsed > 0) return parsed;
  } catch {}
  return Math.floor(Date.now() / 1000);
}

function messageTextAndType(message) {
  const content = unwrapMessageContent(message?.message);
  let body = '';
  let messageType = Object.keys(content)[0] || 'unknown';
  let hasMedia = false;
  let mediaType = null;

  if (content.conversation) body = content.conversation;
  else if (content.extendedTextMessage?.text) body = content.extendedTextMessage.text;
  else if (content.imageMessage) {
    body = content.imageMessage.caption || '';
    hasMedia = true; mediaType = 'image';
  } else if (content.videoMessage) {
    body = content.videoMessage.caption || '';
    hasMedia = true; mediaType = 'video';
  } else if (content.audioMessage || content.pttMessage) {
    hasMedia = true; mediaType = content.pttMessage ? 'ptt' : 'audio';
  } else if (content.documentMessage) {
    body = content.documentMessage.caption || '';
    hasMedia = true; mediaType = 'document';
  } else if (content.stickerMessage) {
    hasMedia = true; mediaType = 'sticker';
  } else if (content.contactMessage || content.contactsArrayMessage) {
    hasMedia = true; mediaType = 'contacts';
  } else if (content.locationMessage || content.liveLocationMessage) {
    hasMedia = true; mediaType = 'location';
  } else if (content.buttonsResponseMessage) {
    body = content.buttonsResponseMessage.selectedDisplayText || '';
  } else if (content.listResponseMessage) {
    body = content.listResponseMessage.title || content.listResponseMessage.description || '';
  }

  return { body: String(body || ''), messageType, hasMedia, mediaType };
}

export function historyRecordFromMessage(message, { historical = false, syncType = null } = {}) {
  const chatId = message?.key?.remoteJid || '';
  const senderId = message?.key?.participant || chatId;
  const parsed = messageTextAndType(message);
  return {
    chat_id: chatId,
    sender_id: senderId,
    sender_name: message?.pushName || '',
    message_id: message?.key?.id || '',
    message_type: parsed.messageType,
    body: parsed.body,
    timestamp: messageTimestamp(message),
    from_me: !!message?.key?.fromMe,
    is_historical: historical,
    has_media: parsed.hasMedia,
    media_type: parsed.mediaType,
    sync_type: syncType,
  };
}

export async function initHistoryStore() {
  const result = await runStore('init');
  historyStats.initialized = !!result.ok;
  return result;
}

export async function persistHistoryBatch(messages, syncType = null) {
  const records = (messages || []).map((message) => historyRecordFromMessage(message, {
    historical: true,
    syncType: syncType == null ? null : String(syncType),
  }));
  if (!records.length) return { ok: true, received: 0, inserted: 0, skipped: 0 };
  const result = await runStore('batch', [], {
    historical: true,
    sync_type: syncType == null ? null : String(syncType),
    records,
  });
  historyStats.batches += 1;
  historyStats.received += Number(result.received || 0);
  historyStats.inserted += Number(result.inserted || 0);
  historyStats.skipped += Number(result.skipped || 0);
  historyStats.lastSyncType = syncType == null ? null : String(syncType);
  historyStats.lastBatchAt = new Date().toISOString();
  historyStats.lastError = null;
  return result;
}

export function persistLiveMessage(message) {
  const record = historyRecordFromMessage(message, { historical: false });
  if (!record.chat_id || !record.message_id) return;
  // Não bloqueia o processamento do bridge nem o envio da resposta.
  runStore('batch', [], { historical: false, records: [record] })
    .catch((err) => {
      historyStats.lastError = err.message;
      console.error(`[history] falha ao persistir mensagem: ${err.message}`);
    });
}

export async function getStoredMessage(key) {
  const chatId = key?.remoteJid || '';
  const messageId = key?.id || '';
  if (!chatId || !messageId) return null;
  try {
    const result = await runStore('get', [chatId, messageId]);
    if (!result.found || !result.body) return null;
    return { conversation: result.body };
  } catch (err) {
    historyStats.lastError = err.message;
    return null;
  }
}

export async function getOldestStoredMessages() {
  try {
    const result = await runStore('oldest');
    return result.chats || [];
  } catch (err) {
    historyStats.lastError = err.message;
    return [];
  }
}

export function getHistoryStats() {
  return { ...historyStats, dbPath: DB_PATH };
}
