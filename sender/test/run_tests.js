// node sender/test/run_tests.js
const fs = require('fs'), path = require('path'), vm = require('vm');
const ctx = vm.createContext({ console, Math, Date, JSON, Object, Array, String, Number, Set, Error, isFinite, Buffer });
['Logic.gs', 'Engine.gs'].forEach(f => vm.runInContext(fs.readFileSync(path.join(__dirname, '..', f), 'utf8'), ctx, { filename: f }));
const L = vm.runInContext(`({COLUMNS, CONFIG_DEFAULTS, mergeConfig, parseHM, addDays, daysBetween, isWeekday, dayOfWeek, parseRamp, rampCap, effectiveCap,
  planSlots, sendsAllowedNow, pickFollowupDate, followupState, classifyBounce, classifyMessage, extractKnownAddresses, killSwitch,
  buildReplyMime, mimeHeaderEncode, isValidEmail, runTick, handleBridge, safeEqual, isSendDay, inWindow})`, ctx);

let pass = 0, fail = 0;
function t(name, fn) { try { fn(); pass++; } catch (e) { fail++; console.log('FAIL', name, '\n    ', e.message); } }
function eq(a, b, m) { if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error((m || '') + ' expected ' + JSON.stringify(b) + ' got ' + JSON.stringify(a)); }
function ok(c, m) { if (!c) throw new Error(m || 'assertion failed'); }
function mulberry(a) { return function () { a |= 0; a = a + 0x6D2B79F5 | 0; let t = Math.imul(a ^ a >>> 15, 1 | a); t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t; return ((t ^ t >>> 14) >>> 0) / 4294967296; }; }
const cfgOf = (o) => L.mergeConfig(Object.assign({}, o));

// ---------------- Logic ----------------
t('config defaults are typed and start paused', () => {
  const c = cfgOf({}); eq(c.PAUSED, true); eq(c.DAILY_CAP, 20); eq(c.WARMUP_START_DATE, ''); eq(c.FOLLOWUP_MAX_DAYS, 8);
});
t('config: strings from the sheet are parsed', () => {
  const c = cfgOf({ DAILY_CAP: '7', PAUSED: 'FALSE', KILL_BOUNCE_RATE: '0.2' }); eq(c.DAILY_CAP, 7); eq(c.PAUSED, false); eq(c.KILL_BOUNCE_RATE, 0.2);
});
t('cap is 0 with a RAMP but no start date (fail closed)', () => eq(L.effectiveCap(cfgOf({ WARMUP_START_DATE: '' }), '2026-10-05'), 0));
t('ramp steps by week from the start date', () => {
  const c = cfgOf({ WARMUP_START_DATE: '2026-10-05' });
  eq(L.effectiveCap(c, '2026-10-04'), 0, 'before start');
  eq(L.effectiveCap(c, '2026-10-05'), 5); eq(L.effectiveCap(c, '2026-10-11'), 5, 'day 6');
  eq(L.effectiveCap(c, '2026-10-12'), 8, 'day 7'); eq(L.effectiveCap(c, '2026-10-19'), 12);
  eq(L.effectiveCap(c, '2026-10-26'), 16); eq(L.effectiveCap(c, '2026-11-02'), 20); eq(L.effectiveCap(c, '2027-03-01'), 20, 'stays at last step');
});
t('DAILY_CAP is a ceiling over the ramp', () => eq(L.effectiveCap(cfgOf({ WARMUP_START_DATE: '2026-10-05', DAILY_CAP: 10 }), '2026-11-09'), 10));
t('empty RAMP means DAILY_CAP alone', () => eq(L.effectiveCap(cfgOf({ RAMP: '', DAILY_CAP: 9, WARMUP_START_DATE: '' }), '2026-10-05'), 9));
t('bad RAMP throws', () => { let threw = false; try { L.parseRamp('1-5'); } catch (e) { threw = true; } ok(threw); });
t('planSlots: count, sorted, in window, min gap, not identical across seeds', () => {
  const a = L.planSlots(20, 540, 990, 12, mulberry(1)), b = L.planSlots(20, 540, 990, 12, mulberry(2));
  eq(a.length, 20); for (let i = 1; i < a.length; i++) ok(a[i] - a[i - 1] >= 12, 'gap');
  ok(a[0] >= 540 && a[19] < 990); ok(JSON.stringify(a) !== JSON.stringify(b));
});
t('planSlots spreads: over many seeds the first and last slots are far apart', () => {
  for (let s = 1; s <= 50; s++) { const p = L.planSlots(5, 540, 990, 12, mulberry(s)); ok(p[4] - p[0] >= 48); }
});
t('planSlots shrinks the gap when infeasible, and handles cap 0', () => {
  const p = L.planSlots(20, 540, 600, 12, mulberry(3)); eq(p.length, 20); ok(p[19] < 600); eq(L.planSlots(0, 540, 990, 12, mulberry(1)), []);
});
t('sendsAllowedNow honours plan, cap and per-run limit', () => {
  eq(L.sendsAllowedNow([540, 560, 580], 0, 5, 530, 1), 0);
  eq(L.sendsAllowedNow([540, 560, 580], 0, 5, 600, 1), 1, 'per-run cap');
  eq(L.sendsAllowedNow([540, 560, 580], 3, 3, 600, 9), 0, 'daily cap reached');
  eq(L.sendsAllowedNow([540, 560, 580], 1, 5, 600, 9), 2);
});
t('follow-up date is ALWAYS a weekday 6 to 8 days out (every weekday, many seeds)', () => {
  for (let day = 0; day < 35; day++) {
    const sent = L.addDays('2026-09-21', day); if (!L.isWeekday(sent)) continue;
    for (let s = 1; s <= 20; s++) {
      const due = L.pickFollowupDate(sent, 6, 8, mulberry(s)), n = L.daysBetween(sent, due);
      ok(L.isWeekday(due), due + ' is a weekend'); ok(n >= 6 && n <= 8, sent + ' -> ' + due + ' = ' + n + ' days');
    }
  }
});
const sentRow = (o) => Object.assign({ status: 'sent', sent_at: '2026-09-21T10:00:00', followup_due: '2026-09-28', followup_body: 'b', thread_id: 't', message_id: 'm', reply_flag: '', bounce_flag: '' }, o);
t('followupState table', () => {
  const c = cfgOf({});
  eq(L.followupState(sentRow({}), '2026-09-25', c), 'not_due'); eq(L.followupState(sentRow({}), '2026-09-28', c), 'eligible');
  eq(L.followupState(sentRow({}), '2026-09-29', c), 'eligible', 'day 8 is still allowed'); eq(L.followupState(sentRow({}), '2026-09-30', c), 'expired', 'day 9, no grace');
  eq(L.followupState(sentRow({ followup_sent_at: '2026-09-28T10:00:00' }), '2026-09-29', c), 'skip', 'only one follow-up');
  eq(L.followupState(sentRow({ reply_flag: 'TRUE' }), '2026-09-28', c), 'skip'); eq(L.followupState(sentRow({ bounce_flag: 'TRUE' }), '2026-09-28', c), 'skip');
  eq(L.followupState(sentRow({ status: 'replied' }), '2026-09-28', c), 'skip'); eq(L.followupState(sentRow({ followup_status: 'sent' }), '2026-09-28', c), 'skip');
  eq(L.followupState(sentRow({ followup_status: 'error' }), '2026-09-28', c), 'skip', 'never retry an errored follow-up'); eq(L.followupState(sentRow({ followup_status: 'sending' }), '2026-09-28', c), 'skip', 'never retry an ambiguous follow-up');
});
t('classifyMessage', () => {
  eq(L.classifyMessage({ from: 'Mail Delivery Subsystem <mailer-daemon@googlemail.com>', subject: 'Delivery Status Notification (Failure)', body: '550 5.1.1 The email account that you tried to reach does not exist' }), 'bounce_hard');
  eq(L.classifyMessage({ from: 'postmaster@x.com', subject: 'Undeliverable', body: '550 5.7.1 message blocked due to spam reputation' }), 'bounce_block');
  eq(L.classifyMessage({ from: 'mailer-daemon@x.com', subject: 'Delayed', body: '452 4.2.2 mailbox full, try again later' }), 'bounce_soft');
  eq(L.classifyMessage({ from: 'a@b.com', subject: 'Automatic reply: hi', body: 'I am out of the office until Monday' }), 'auto_reply');
  eq(L.classifyMessage({ from: 'a@b.com', subject: 'Re: x', body: 'Please remove me from your list.' }), 'opt_out');
  eq(L.classifyMessage({ from: 'a@b.com', subject: 'Re: x', body: 'Happy to chat, how is Thursday?' }), 'reply');
  eq(L.classifyMessage({ from: 'a@b.com', subject: 'Re: x', body: 'hello', autoSubmitted: 'auto-replied' }), 'auto_reply');
});
t('extractKnownAddresses only returns recipients we sent to', () => {
  const known = new Set(['jane.doe@acme.org']);
  eq(L.extractKnownAddresses('Your message to Jane.Doe@acme.org failed. postmaster@acme.org other@x.com', known), ['jane.doe@acme.org']);
});
t('killSwitch', () => {
  const c = cfgOf({}), send = (b) => ({ bounce_type: b });
  const clean = Array.from({ length: 20 }, () => send(''));
  eq(L.killSwitch(clean, c, { bouncesToday: 0, optOuts: 0, consecutiveErrors: 0 }).pause, false);
  const one = clean.slice(); one[5] = send('hard'); eq(L.killSwitch(one, c, { bouncesToday: 0, optOuts: 0, consecutiveErrors: 0 }).pause, false, '1 of 20 is not a spike');
  const two = one.slice(); two[9] = send('hard'); eq(L.killSwitch(two, c, { bouncesToday: 0, optOuts: 0, consecutiveErrors: 0 }).pause, true, '2 of 20 = 10%');
  eq(L.killSwitch([send('hard'), send('hard')], c, { bouncesToday: 0, optOuts: 0, consecutiveErrors: 0 }).pause, false, 'tiny sample by rate');
  eq(L.killSwitch(clean, c, { bouncesToday: 3, optOuts: 0, consecutiveErrors: 0 }).pause, true, '3 today');
  const blk = clean.slice(); blk[1] = send('block'); blk[2] = send('block'); eq(L.killSwitch(blk, c, { bouncesToday: 0, optOuts: 0, consecutiveErrors: 0 }).pause, true, 'blocks');
  const soft = clean.map(() => send('soft')); eq(L.killSwitch(soft, c, { bouncesToday: 0, optOuts: 0, consecutiveErrors: 0 }).pause, false, 'soft never counts');
  eq(L.killSwitch(clean, c, { bouncesToday: 0, optOuts: 2, consecutiveErrors: 0 }).pause, true, 'opt-outs');
  eq(L.killSwitch(clean, c, { bouncesToday: 0, optOuts: 0, consecutiveErrors: 3 }).pause, true, 'errors');
});
t('reply MIME threads correctly and blocks header injection', () => {
  const b64 = s => Buffer.from(s, 'utf8').toString('base64');
  const m = L.buildReplyMime({ from: '"T" <t@example.com>', to: 'a@b.com', subject: 'Re: hi\r\nBcc: evil@x.com', inReplyTo: '<abc@mail.gmail.com>', body: 'Body' }, b64);
  ok(/In-Reply-To: <abc@mail.gmail.com>/.test(m)); ok(/References: <abc@mail.gmail.com>/.test(m)); ok(!/\r\nBcc:/.test(m), 'header injection'); ok(m.endsWith('\r\n\r\nBody'));
  ok(/^Subject: =\?UTF-8\?B\?/m.test(L.buildReplyMime({ to: 'a@b.com', subject: 'Re: café', inReplyTo: '<x>', body: 'b' }, b64)), 'non-ascii subject encoded');
});
t('isValidEmail', () => { ok(L.isValidEmail('a@b.co')); ok(!L.isValidEmail('a@b')); ok(!L.isValidEmail('a b@c.com')); });
t('columns match the Python side', () => {
  const py = fs.readFileSync(path.join(__dirname, '..', '..', 'bridge.py'), 'utf8');
  const m = /SHEET_COLUMNS = \[([\s\S]*?)\]/.exec(py)[1].match(/"(\w+)"/g).map(s => s.replace(/"/g, ''));
  eq(m, L.COLUMNS);
});

// ---------------- Engine (fake Gmail, fake sheet, controllable clock) ----------------
function world(opts) {
  opts = opts || {};
  const w = { rows: [], suppress: new Set(), props: {}, config: Object.assign({ PAUSED: 'FALSE', WARMUP_START_DATE: '2026-09-14', DAILY_CAP: 20, RAMP: opts.ramp === undefined ? '1:5,2:8,3:12,4:16,5:20' : opts.ramp }, opts.config || {}),
    clock: { date: '2026-09-21', minutes: 0 }, sends: [], followups: [], alerts: [], logs: [], threads: {}, bounces: [], failSend: false, failUpdateToSent: false, rng: mulberry(opts.seed || 7) };
  let n = 0;
  w.queueRow = (o) => { n++; const r = Object.assign({ _row: 0, id: 'r' + n, email: 'p' + n + '@org' + n + '.com', name: 'P' + n, firm: 'F', subject: 'Subj ' + n, body: 'Body ' + n, followup_body: 'FU ' + n, status: 'queued', queued_at: '2026-09-19T00:00:' + ('0' + n).slice(-2) }, o); r._row = w.rows.length + 2; w.rows.push(r); return r; };
  w.d = {
    config: { load: () => L.mergeConfig(Object.assign({}, w.config)), set: (k, v) => { w.config[k] = String(v); } },
    now: () => ({ date: w.clock.date, minutes: w.clock.minutes }),
    parts: (x) => ({ date: x.date, minutes: x.minutes, iso: x.date + 'T' + ('0' + Math.floor(x.minutes / 60)).slice(-2) + ':' + ('0' + (x.minutes % 60)).slice(-2) + ':00' }),
    rng: w.rng,
    props: { get: k => (k in w.props ? w.props[k] : null), set: (k, v) => { w.props[k] = String(v); } },
    queue: { rows: () => w.rows, update: (row, f) => { if (w.failUpdateToSent && f.status === 'sent') { w.failUpdateToSent = false; throw new Error('sheet write failed'); } Object.assign(row, f); }, append: (o) => o.forEach(x => w.queueRow(x)) },
    suppress: { has: e => w.suppress.has(String(e).toLowerCase()), add: e => w.suppress.add(String(e).toLowerCase()) },
    gmail: {
      myEmail: () => 'sender@example.com',
      sendFirst: (row) => { if (w.failSend) throw new Error('quota'); const id = 't-' + row.id; w.sends.push({ id: row.id, at: w.clock.date + ' ' + w.clock.minutes }); w.threads[id] = [{ from: 'sender@example.com', subject: row.subject, body: row.body }]; return { threadId: id, messageId: 'm-' + row.id }; },
      sendFollowup: (row) => { if (w.failSend) throw new Error('quota'); w.followups.push({ id: row.id, thread: row.thread_id, at: w.clock.date }); return { messageId: 'f-' + row.id }; },
      threadMessages: (id) => w.threads[id] || [],
      searchBounces: () => w.bounces,
    },
    alert: (s, b) => w.alerts.push(s), log: (e, dd) => w.logs.push(e + ' ' + dd),
  };
  // tick through a whole day every 10 minutes
  w.day = (date) => { const results = []; w.clock.date = date; for (let m = 0; m < 24 * 60; m += 10) { w.clock.minutes = m; results.push(L.runTick(w.d)); } return results; };
  return w;
}
const MON = '2026-09-21', TUE = '2026-09-22', SAT = '2026-09-26', SUN = '2026-09-27', MON2 = '2026-09-28';

t('engine: paused sends nothing', () => { const w = world({ config: { PAUSED: 'TRUE' } }); for (let i = 0; i < 10; i++) w.queueRow(); w.day(MON); eq(w.sends.length, 0); });
t('engine: no start date => cap 0 => nothing sends', () => { const w = world({ config: { WARMUP_START_DATE: '' } }); for (let i = 0; i < 10; i++) w.queueRow(); w.day(MON); eq(w.sends.length, 0); });
t('engine: weekend sends nothing, weekday sends exactly the ramp cap, inside the window, spread, one per tick', () => {
  const w = world({ config: { WARMUP_START_DATE: '2026-09-21' } });   // Monday = week 1 = cap 5
  for (let i = 0; i < 40; i++) w.queueRow();
  w.day(SAT); w.day(SUN); eq(w.sends.length, 0, 'weekend');
  w.day(MON); eq(w.sends.length, 5, 'week-1 cap');
  const mins = w.sends.map(s => Number(s.at.split(' ')[1]));
  ok(mins.every(m => m >= 540 && m < 990), 'inside 09:00-16:30 ' + mins);
  for (let i = 1; i < mins.length; i++) ok(mins[i] - mins[i - 1] >= 10, 'no burst ' + mins);
});
t('engine: raising DAILY_CAP mid-ramp is obeyed next day; lowering is obeyed immediately', () => {
  const w = world({ config: { WARMUP_START_DATE: '2026-09-21', DAILY_CAP: 3 } }); for (let i = 0; i < 60; i++) w.queueRow();
  w.day(MON); eq(w.sends.length, 3, 'ceiling 3 under ramp 5');
  w.config.DAILY_CAP = '20'; w.day(TUE); eq(w.sends.length, 3 + 5, 'ramp 5 now binds');
});
t('engine: the cap can reach 20/day at the end of the ramp', () => {
  const w = world({ config: { WARMUP_START_DATE: '2026-08-17' } }); for (let i = 0; i < 60; i++) w.queueRow();
  w.day(MON); eq(w.sends.length, 20);
});
t('engine: a send error is recorded, not retried, and 3 in a row trips the kill switch', () => {
  const w = world({ config: { WARMUP_START_DATE: '2026-09-21' } }); for (let i = 0; i < 20; i++) w.queueRow(); w.failSend = true;
  const res = w.day(MON); eq(w.sends.length, 0);
  eq(w.rows.filter(r => r.status === 'error').length, 3, 'exactly 3 attempts then paused'); eq(w.config.PAUSED, 'TRUE'); ok(w.alerts.length === 1, 'one alert');
  ok(res.some(r => r.action === 'killed'));
});
t('engine: crash after send but before the sheet write leaves the row in "sending" and never re-sends it', () => {
  const w = world({ config: { WARMUP_START_DATE: '2026-09-21' } }); const r = w.queueRow(); for (let i = 0; i < 6; i++) w.queueRow();
  w.failUpdateToSent = true; w.day(MON);
  eq(r.status, 'sending'); eq(w.sends.filter(s => s.id === r.id).length, 1, 'sent once'); eq(w.config.PAUSED, 'TRUE', 'paused because state is inconsistent'); ok(w.alerts.length === 1); w.config.PAUSED = 'FALSE'; w.day(TUE); eq(w.sends.filter(s => s.id === r.id).length, 1, 'still once even after resuming');
});
t('engine: suppression list and duplicate addresses are never sent', () => {
  const w = world({ config: { WARMUP_START_DATE: '2026-09-21' } });
  const a = w.queueRow({ email: 'no@x.com' }); w.suppress.add('no@x.com'); const b = w.queueRow({ email: 'dup@x.com' }); const c = w.queueRow({ email: 'DUP@x.com' });
  for (let i = 0; i < 4; i++) w.queueRow(); w.day(MON);
  eq(a.status, 'suppressed'); eq(w.sends.filter(s => s.id === b.id || s.id === c.id).length, 1, 'address sent to once');
});
t('engine: ONE follow-up, 6-8 days later, in the same thread, no reply -> then never again', () => {
  const w = world({ config: { WARMUP_START_DATE: '2026-09-21' } }); const r = w.queueRow(); for (let i = 0; i < 5; i++) w.queueRow();
  w.day(MON); eq(r.status, 'sent'); const n = L.daysBetween(MON, r.followup_due); ok(n >= 6 && n <= 8 && L.isWeekday(r.followup_due), 'due ' + r.followup_due);
  for (let d = 1; d < 40; d++) w.day(L.addDays(MON, d));
  eq(w.followups.filter(f => f.id === r.id).length, 1, 'exactly one follow-up'); eq(w.followups.find(f => f.id === r.id).thread, 't-' + r.id, 'same thread');
  ok(w.followups.find(f => f.id === r.id).at === r.followup_due, 'sent on the due date'); eq(r.followup_status, 'sent');
});
t('engine: if recording a follow-up fails, it pauses and NEVER sends a second follow-up', () => {
  const w = world({ config: { WARMUP_START_DATE: '2026-09-21' } }); const r = w.queueRow(); for (let i = 0; i < 5; i++) w.queueRow(); w.day(MON);
  const realUpdate = w.d.queue.update; let boom = true; w.d.queue.update = (row, f) => { if (boom && f.followup_status === 'sent') { boom = false; throw new Error('sheet write failed'); } realUpdate(row, f); };
  w.day(r.followup_due); eq(w.followups.filter(f => f.id === r.id).length, 1); eq(w.config.PAUSED, 'TRUE');
  w.config.PAUSED = 'FALSE'; for (let d = 1; d < 12; d++) w.day(L.addDays(r.followup_due, d)); eq(w.followups.filter(f => f.id === r.id).length, 1, 'still exactly one');
});
t('engine: a reply before the due date cancels the follow-up', () => {
  const w = world({ config: { WARMUP_START_DATE: '2026-09-21' } }); const r = w.queueRow(); for (let i = 0; i < 5; i++) w.queueRow(); w.day(MON);
  w.threads['t-' + r.id].push({ from: 'Them <them@x.com>', subject: 'Re', body: 'Sure, Thursday works.' });
  for (let d = 1; d < 15; d++) w.day(L.addDays(MON, d));
  eq(r.status, 'replied'); eq(r.reply_flag, 'TRUE'); eq(w.followups.filter(f => f.id === r.id).length, 0);
});
t('engine: an opt-out reply suppresses the person, and an auto-reply does not count as a reply', () => {
  const w = world({ config: { WARMUP_START_DATE: '2026-09-21' } }); const a = w.queueRow(), b = w.queueRow(); for (let i = 0; i < 4; i++) w.queueRow(); w.day(MON);
  w.threads['t-' + a.id].push({ from: 'a@x.com', subject: 'Re', body: 'Please stop emailing me.' });
  w.threads['t-' + b.id].push({ from: 'b@x.com', subject: 'Automatic reply', body: 'I am out of the office until Monday' });
  w.day(TUE); eq(a.status, 'suppressed'); ok(w.suppress.has(a.email)); eq(b.status, 'sent', 'auto reply ignored');
});
t('engine: a bounce marks the row, suppresses the address and cancels the follow-up', () => {
  const w = world({ config: { WARMUP_START_DATE: '2026-09-21' } }); const r = w.queueRow(); for (let i = 0; i < 5; i++) w.queueRow(); w.day(MON);
  w.bounces = [{ id: 'b1', from: 'mailer-daemon@googlemail.com', subject: 'Delivery Status Notification (Failure)', body: 'Address not found. Your message to ' + r.email + ' was not delivered. 550 5.1.1 does not exist' }];
  w.day(TUE); eq(r.status, 'bounced'); eq(r.bounce_flag, 'TRUE'); eq(r.error, 'bounce:hard'); ok(w.suppress.has(r.email));
  for (let d = 1; d < 15; d++) w.day(L.addDays(MON, d)); eq(w.followups.filter(f => f.id === r.id).length, 0);
});
t('engine: a follow-up that would land after day 8 is dropped, never sent late', () => {
  const w = world({ config: { WARMUP_START_DATE: '2026-09-21', PAUSED: 'FALSE' } }); const r = w.queueRow(); for (let i = 0; i < 5; i++) w.queueRow(); w.day(MON);
  w.config.PAUSED = 'TRUE'; w.day(L.addDays(MON, 3)); w.clock.date = L.addDays(MON, 12); w.config.PAUSED = 'FALSE';
  for (let d = 12; d < 20; d++) w.day(L.addDays(MON, d)); eq(w.followups.filter(f => f.id === r.id).length, 0); eq(r.followup_status, 'expired');
});
t('engine: follow-ups count against the daily cap', () => {
  const w = world({ config: { WARMUP_START_DATE: '2026-09-21', DAILY_CAP: 5 } }); for (let i = 0; i < 30; i++) w.queueRow(); w.day(MON);   // 5 first sends
  const sent = w.rows.filter(r => r.status === 'sent'); const due = sent.map(r => r.followup_due).sort();
  w.sends.length = 0; w.followups.length = 0; w.day(due[0]);
  eq(w.sends.length + w.followups.length <= 8, true); ok(w.sends.length + w.followups.length <= 8, 'ramp week 2 cap');
});
t('engine: kill switch on a bounce spike pauses, alerts once, and stops all sending', () => {
  const w = world({ config: { WARMUP_START_DATE: '2026-08-17' } }); for (let i = 0; i < 60; i++) w.queueRow();
  w.day(MON); const first = w.sends.length; eq(first, 20);
  const victims = w.rows.filter(r => r.status === 'sent').slice(0, 2);
  w.bounces = victims.map((v, i) => ({ id: 'bb' + i, from: 'mailer-daemon@x.com', subject: 'Failure', body: '550 5.1.1 ' + v.email + ' does not exist' }));
  w.day(TUE); eq(w.config.PAUSED, 'TRUE'); eq(w.alerts.length, 1); eq(w.sends.length, first, 'nothing more sent after the spike'); ok(/bounce rate/.test(w.config.PAUSE_REASON));
  w.day(L.addDays(TUE, 1)); eq(w.sends.length, first, 'still paused'); eq(w.alerts.length, 1, 'alerted once');
});
t('bridge: rejects a bad token, dedupes, respects suppression, exports status', () => {
  const w = world(); w.suppress.add('s@x.com'); w.queueRow({ email: 'have@x.com' });
  eq(L.handleBridge(w.d, { token: 'nope', action: 'status' }, 'secret').ok, false); eq(L.handleBridge(w.d, { token: 'x', action: 'status' }, '').ok, false, 'no token configured = closed');
  const r = L.handleBridge(w.d, { token: 'secret', action: 'append', rows: [
    { email: 'new@x.com', subject: 's', body: 'b' }, { email: 'HAVE@x.com', subject: 's', body: 'b' }, { email: 's@x.com', subject: 's', body: 'b' },
    { email: 'bad', subject: 's', body: 'b' }, { email: 'nobody@x.com', subject: '', body: 'b' }, { email: 'new@x.com', subject: 's', body: 'b' }] }, 'secret');
  eq(r.added, 1); eq(r.skipped.map(s => s.reason), ['already in sheet', 'on suppression list', 'invalid email', 'missing subject or body', 'already in sheet']);
  eq(L.handleBridge(w.d, { token: 'secret', action: 'export' }, 'secret').rows.length, 2);
});

console.log(pass + ' passed, ' + fail + ' failed'); process.exit(fail ? 1 : 0);
