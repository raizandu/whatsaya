import test, { after, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';

const testHome = mkdtempSync(join(tmpdir(), 'whatsaya-campaign-metadata-'));
process.env.HOME = testHome;
process.env.WHATSAPP_ALLOWED_USERS = '*';
process.env.WHATSAPP_MODE = 'bot';
process.env.WHATSAPP_DEBOUNCE_INITIAL_MS = '0';
process.env.WHATSAPP_HISTORY_PERSIST_DISABLED = 'true';
process.env.WHATSAPP_GROUPS_ENABLED = 'true';

const bridge = await import('../bridge.js');

bridge.setSock({
  user: { id: '19999999999:1@s.whatsapp.net', lid: '111:1@lid' },
  contacts: {},
});

beforeEach(() => {
  bridge.clearRecentlyProcessedIds();
  bridge.getMessageQueue().length = 0;
});

after(() => {
  rmSync(testHome, { recursive: true, force: true });
});

test('native CTWA campaign maps exactly to US market while keeping Spanish', () => {
  const metadata = bridge.extractLeadMetadata(
    {
      smbClientCampaignId: 'campaign-us-es',
      utm: { utmSource: 'native-meta-source' },
      leadMetadata: {
        country: 'Brazil',
        currency: 'BRL',
        offer: 'brazil',
        unknown: 'must-not-cross-the-bridge',
      },
      externalAdReply: {
        title: 'untrusted ad copy',
        body: 'Brazil BRL Pix',
        sourceUrl: 'https://untrusted.example',
      },
    },
    {
      'campaign-us-es': {
        market_id: 'US',
        language: 'es',
        timezone: 'America/New_York',
        origin: 'meta_ads',
      },
    },
  );

  assert.deepEqual(metadata, {
    origin: 'meta_ads',
    campaign: 'campaign-us-es',
    market_id: 'US',
    language: 'es',
    timezone: 'America/New_York',
  });
  assert.equal(metadata.currency, undefined);
  assert.equal(metadata.offer, undefined);
  assert.equal(metadata.country, undefined);
  assert.equal(metadata.unknown, undefined);
});

test('campaign lookup never uses substrings or ad copy to infer market', () => {
  const metadata = bridge.extractLeadMetadata(
    {
      smbClientCampaignId: 'campaign-us-es-extra',
      conversionSource: 'ctwa',
      externalAdReply: { title: 'US Spanish cleaning campaign' },
    },
    {
      'campaign-us-es': { market_id: 'US', language: 'es' },
    },
  );

  assert.deepEqual(metadata, {
    origin: 'ctwa',
    campaign: 'campaign-us-es-extra',
  });
});

test('resolved delivery IDs retain the original LID aliases', async () => {
  const textMessage = (id, key) => ({
    key: { id, fromMe: false, ...key },
    messageTimestamp: 1787490000,
    message: { conversation: 'Hola' },
  });

  await bridge.onMessagesUpsert({
    type: 'notify',
    messages: [
      textMessage('pn-primary', {
        remoteJid: '15551234567@s.whatsapp.net',
        remoteJidAlt: '987654321@lid',
      }),
      textMessage('lid-primary', {
        remoteJid: '222222222@lid',
        remoteJidAlt: '15557654321@s.whatsapp.net',
      }),
      textMessage('group-alt', {
        remoteJid: '12345@g.us',
        participant: '15559876543@s.whatsapp.net',
        participantAlt: '333333333@lid',
      }),
    ],
  });

  const [pnPrimary, lidPrimary, groupAlt] = bridge.getMessageQueue();
  assert.deepEqual(
    [pnPrimary.chatId, pnPrimary.senderId, pnPrimary.originalChatId, pnPrimary.originalSenderId],
    [
      '15551234567@s.whatsapp.net',
      '15551234567@s.whatsapp.net',
      '987654321@lid',
      '987654321@lid',
    ],
  );
  assert.deepEqual(
    [lidPrimary.chatId, lidPrimary.senderId, lidPrimary.originalChatId, lidPrimary.originalSenderId],
    [
      '15557654321@s.whatsapp.net',
      '15557654321@s.whatsapp.net',
      '222222222@lid',
      '222222222@lid',
    ],
  );
  assert.deepEqual(
    [groupAlt.chatId, groupAlt.senderId, groupAlt.originalChatId, groupAlt.originalSenderId],
    ['12345@g.us', '15559876543@s.whatsapp.net', undefined, '333333333@lid'],
  );
});
