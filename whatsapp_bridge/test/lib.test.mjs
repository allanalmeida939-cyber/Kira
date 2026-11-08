import test from 'node:test';
import assert from 'node:assert/strict';
import { isOwnChat, Inbox, mediaKind, messageText, normalizePhone, resolveContact } from '../lib.mjs';

const kiraContacts = [{ name: 'Amye', aliases: ['amy', 'amie'], phone: '(85) 98888-7777', whatsapp_name: 'Amye' }];
const synced = new Map([
  ['5585911112222@s.whatsapp.net', { name: 'Breno Silva', notify: 'Breno' }],
  ['5585933334444@s.whatsapp.net', { name: 'Larissa', notify: '' }],
  ['5585955556666@s.whatsapp.net', { name: 'Larissa Trabalho', notify: '' }],
  ['5585977778888@s.whatsapp.net', { name: '', notify: 'Mãe' }],
]);

test('telefone ganha DDI do Brasil e vira JID', () => {
  assert.equal(normalizePhone('(85) 99999-1234'), '5585999991234');
  assert.equal(normalizePhone('+55 85 99999-1234'), '5585999991234');
  assert.equal(normalizePhone('123'), null);
  assert.deepEqual(resolveContact('85 99999-1234'), { phone: '5585999991234', jid: '5585999991234@s.whatsapp.net', name: '85 99999-1234' });
});

test('apelido da lista da Kira vale mais que o WhatsApp', () => {
  assert.deepEqual(resolveContact('amy', { kiraContacts, synced }), { phone: '5585988887777', jid: '5585988887777@s.whatsapp.net', name: 'Amye' });
});

test('contato sincronizado: nome exato, primeiro nome, acento e ambiguidade', () => {
  assert.equal(resolveContact('breno', { kiraContacts, synced }).name, 'Breno Silva');
  assert.equal(resolveContact('Larissa', { kiraContacts, synced }).name, 'Larissa', 'nome exato ganha de "Larissa Trabalho"');
  assert.equal(resolveContact('mae', { kiraContacts, synced }).jid, '5585977778888@s.whatsapp.net');
  const many = resolveContact('lari', { kiraContacts, synced });
  assert.match(many.error, /mais de um contato com esse nome .*diga o final do número/);
  assert.deepEqual(many.candidates, ['Larissa final 4444', 'Larissa Trabalho final 6666']);
  assert.equal(resolveContact('Zé Ninguém', { kiraContacts, synced }).error, 'contato não encontrado');
});

test('dois contatos com o mesmo nome: final do número ou conversa recente decidem', () => {
  const day = 24 * 3600;
  const twins = new Map([
    ['5585911111234@s.whatsapp.net', { name: 'Breno', notify: '', last: 0 }],
    ['5585922225678@s.whatsapp.net', { name: 'Breno', notify: '', last: 0 }],
  ]);
  assert.deepEqual(resolveContact('breno', { synced: twins }).candidates, ['Breno final 1234', 'Breno final 5678']);
  assert.equal(resolveContact('Breno final 5678', { synced: twins }).jid, '5585922225678@s.whatsapp.net');
  assert.equal(resolveContact('breno 1234', { synced: twins }).jid, '5585911111234@s.whatsapp.net');
  twins.get('5585922225678@s.whatsapp.net').last = 1_800_000_000;
  assert.equal(resolveContact('breno', { synced: twins }).jid, '5585922225678@s.whatsapp.net', 'só um tem conversa');
  twins.get('5585911111234@s.whatsapp.net').last = 1_800_000_000 - 2 * day;
  assert.ok(resolveContact('breno', { synced: twins }).error, 'os dois conversam: pergunta');
});

test('texto da mensagem e etiquetas de mídia', () => {
  assert.equal(messageText({ conversation: 'oi' }), 'oi');
  assert.equal(messageText({ extendedTextMessage: { text: 'link' } }), 'link');
  assert.equal(messageText({ imageMessage: { caption: 'olha isso' } }), '[foto] olha isso');
  assert.equal(messageText({ audioMessage: {} }), '[áudio]');
  assert.equal(messageText({ ephemeralMessage: { message: { conversation: 'some' } } }), 'some');
  assert.equal(messageText({ protocolMessage: {} }), null);
});

test('tipo de mídia pela extensão', () => {
  assert.equal(mediaKind('C:/a/print.PNG'), 'image');
  assert.equal(mediaKind('C:/a/video.mp4'), 'video');
  assert.equal(mediaKind('C:/a/musica.mp3'), 'audio');
  assert.equal(mediaKind('C:/a/relatorio.pdf'), 'document');
});

test('caixa de entrada tem limite e é consumida', () => {
  const inbox = new Inbox(2);
  inbox.push(1); inbox.push(2); inbox.push(3);
  assert.deepEqual(inbox.drain(), [2, 3]);
  assert.deepEqual(inbox.drain(), []);
});

test('número da própria conta sai do id do Baileys', async () => {
  const { phoneFromJid } = await import('../lib.mjs');
  assert.equal(phoneFromJid('5585912345678:12@s.whatsapp.net'), '5585912345678');
  assert.equal(phoneFromJid('5585912345678@s.whatsapp.net'), '5585912345678');
  assert.equal(phoneFromJid(undefined), null);
});

test('mesmo celular com e sem o nono dígito', async () => {
  const { samePhone } = await import('../lib.mjs');
  assert.ok(samePhone('(85) 98888-7777', '5585988887777'));
  assert.ok(samePhone('5585988887777', '558588887777'), 'conta antiga do WhatsApp, sem o 9');
  assert.ok(!samePhone('5585988887777', '5585988887776'));
  assert.ok(!samePhone('', '5585988887777'));
});

test('secretária: quem da lista da Kira escreveu (número, ou nome salvo na agenda; nunca o nome que a pessoa escolhe)', async () => {
  const { kiraContactFor } = await import('../lib.mjs');
  const list = [
    { name: 'Amye', aliases: ['amy'], phone: '', number: '', whatsapp_name: 'Amye' },
    { name: 'Breno', phone: '(85) 91111-2222' },
  ];
  const book = new Map([
    ['5585933334444@s.whatsapp.net', { name: 'Amye ❤️', notify: 'Amy' }],
    ['5585977778888@s.whatsapp.net', { name: '', notify: 'Amye' }],
  ]);
  assert.equal(kiraContactFor('5585911112222@s.whatsapp.net', { kiraContacts: list, synced: book }), 'Breno', 'pelo número da lista');
  assert.equal(kiraContactFor('558511112222@s.whatsapp.net', { kiraContacts: list, synced: book }), 'Breno', 'sem o nono dígito');
  assert.equal(kiraContactFor('5585933334444@s.whatsapp.net', { kiraContacts: list, synced: book }), 'Amye', 'sem número: pelo nome da agenda');
  assert.equal(kiraContactFor('5585977778888@s.whatsapp.net', { kiraContacts: list, synced: book }), null, 'o nome que a própria pessoa põe não vale');
  assert.equal(kiraContactFor('5585900000000@s.whatsapp.net', { kiraContacts: list, synced: book }), null, 'fora da lista');
  assert.equal(kiraContactFor('123456789@lid', { kiraContacts: list, synced: book }), null, 'sem número de telefone');
  // duas "Amye" na agenda: não dá para saber qual é, então ninguém passa
  const twins = new Map([...book, ['5585955556666@s.whatsapp.net', { name: 'Amye', notify: '' }], ['5585966667777@s.whatsapp.net', { name: 'Amye', notify: '' }]]);
  assert.equal(kiraContactFor('5585955556666@s.whatsapp.net', { kiraContacts: list, synced: twins }), null);
});

test('isOwnChat reconhece o chat próprio por s.whatsapp.net e por @lid', () => {
  const user = { id: '5511999998888:12@s.whatsapp.net', lid: '123456789012345:12@lid' };
  assert.equal(isOwnChat({ remoteJid: '5511999998888@s.whatsapp.net' }, user), true);
  assert.equal(isOwnChat({ remoteJid: '123456789012345@lid' }, user), true);
  assert.equal(isOwnChat({ remoteJid: '999@lid', remoteJidAlt: '5511999998888@s.whatsapp.net' }, user), true);
  assert.equal(isOwnChat({ remoteJid: '5511888887777@s.whatsapp.net' }, user), false);
  assert.equal(isOwnChat({ remoteJid: '123456789012345@lid' }, { id: user.id }), false);
});
