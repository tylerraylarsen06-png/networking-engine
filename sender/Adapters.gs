/**
 * Adapters.gs: the only file that touches Google services for the sender. Container-bound to the sheet.
 * Everything decision-related lives in Logic.gs and Engine.gs.
 */

const TAB_QUEUE = 'Queue';
const TAB_CONFIG = 'Config';
const TAB_SUPPRESS = 'Suppress';
const TAB_LOG = 'Log';

function makeDeps() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const props = PropertiesService.getScriptProperties();
  const cfgSheet = ss.getSheetByName(TAB_CONFIG);
  const qSheet = ss.getSheetByName(TAB_QUEUE);
  const sSheet = ss.getSheetByName(TAB_SUPPRESS);
  const logSheet = ss.getSheetByName(TAB_LOG);
  if (!cfgSheet || !qSheet || !sSheet || !logSheet) throw new Error('Run Outreach > Set up sheet first.');

  let cachedCfg = null;
  function loadRawConfig() {
    const raw = {};
    const vals = cfgSheet.getDataRange().getDisplayValues();
    for (let i = 1; i < vals.length; i++) if (vals[i][0]) raw[String(vals[i][0]).trim()] = String(vals[i][1]).trim();
    return raw;
  }
  function cfg() {
    if (!cachedCfg) cachedCfg = mergeConfig(loadRawConfig());
    return cachedCfg;
  }

  const suppressed = {};
  (function () {
    const vals = sSheet.getDataRange().getDisplayValues();
    for (let i = 1; i < vals.length; i++) if (vals[i][0]) suppressed[String(vals[i][0]).trim().toLowerCase()] = true;
  })();

  function readQueue() {
    const vals = qSheet.getDataRange().getDisplayValues();
    if (!vals.length) return [];
    const header = vals[0].map(String);
    if (header.join('|') !== COLUMNS.join('|')) throw new Error('Queue header does not match the expected columns. Run Outreach > Set up sheet.');
    const out = [];
    for (let i = 1; i < vals.length; i++) {
      if (!vals[i][1] && !vals[i][0]) continue;          // blank line
      const row = { _row: i + 1 };
      COLUMNS.forEach(function (c, j) { row[c] = vals[i][j]; });
      out.push(row);
    }
    return out;
  }

  return {
    config: {
      load: function () { cachedCfg = null; return cfg(); },
      set: function (key, value) {
        const vals = cfgSheet.getDataRange().getValues();
        for (let i = 1; i < vals.length; i++) {
          if (String(vals[i][0]).trim() === key) { cfgSheet.getRange(i + 1, 2).setValue(value); cachedCfg = null; return; }
        }
        cfgSheet.appendRow([key, value]);
        cachedCfg = null;
      },
    },
    now: function () { return new Date(); },
    parts: function (date) {
      const iso = Utilities.formatDate(date, cfg().TIMEZONE, "yyyy-MM-dd'T'HH:mm:ss");
      return { date: iso.slice(0, 10), minutes: Number(iso.slice(11, 13)) * 60 + Number(iso.slice(14, 16)), iso: iso };
    },
    rng: Math.random,
    props: {
      get: function (k) { return props.getProperty(k); },
      set: function (k, v) { props.setProperty(k, String(v)); },
    },
    queue: {
      rows: readQueue,
      update: function (row, fields) {
        Object.keys(fields).forEach(function (k) { row[k] = fields[k]; });
        qSheet.getRange(row._row, 1, 1, COLUMNS.length).setValues([COLUMNS.map(function (c) { return row[c] == null ? '' : row[c]; })]);
      },
      append: function (objs) {
        const start = qSheet.getLastRow() + 1;
        const data = objs.map(function (o) { return COLUMNS.map(function (c) { return o[c] == null ? '' : String(o[c]); }); });
        qSheet.getRange(start, 1, data.length, COLUMNS.length).setValues(data);
      },
    },
    suppress: {
      has: function (email) { return !!suppressed[String(email).trim().toLowerCase()]; },
      add: function (email, reason) {
        const e = String(email).trim().toLowerCase();
        if (suppressed[e]) return;
        suppressed[e] = true;
        sSheet.appendRow([e, reason || '', new Date().toISOString()]);
      },
    },
    gmail: {
      myEmail: function () { return Session.getEffectiveUser().getEmail(); },
      sendFirst: function (row, c) {
        const draft = GmailApp.createDraft(row.email, row.subject, row.body, { name: c.SENDER_NAME });
        const msg = draft.send();                              // returns the sent GmailMessage
        return { messageId: msg.getId(), threadId: msg.getThread().getId() };
      },
      sendFollowup: function (row, c) {
        const orig = GmailApp.getMessageById(row.message_id);
        const mid = orig.getHeader('Message-ID');
        if (!mid) throw new Error('original Message-ID header unavailable; refusing to send an unthreaded follow-up');
        const me = Session.getEffectiveUser().getEmail();
        const from = c.SENDER_NAME ? '"' + c.SENDER_NAME.replace(/"/g, '') + '" <' + me + '>' : me;
        const subject = 'Re: ' + String(orig.getSubject()).replace(/^\s*re:\s*/i, '');
        const b64 = function (s) { return Utilities.base64Encode(Utilities.newBlob(s).getBytes()); };
        const mime = buildReplyMime({ from: from, to: row.email, subject: subject, inReplyTo: mid, references: mid, body: row.followup_body }, b64);
        const raw = Utilities.base64EncodeWebSafe(Utilities.newBlob(mime).getBytes());
        const out = Gmail.Users.Messages.send({ raw: raw, threadId: row.thread_id }, 'me');
        return { messageId: out.id };
      },
      threadMessages: function (threadId) {
        return GmailApp.getThreadById(threadId).getMessages().map(function (m) {
          return { id: m.getId(), from: m.getFrom(), subject: m.getSubject(), body: String(m.getPlainBody()).slice(0, 4000) };
        });
      },
      searchBounces: function () {
        const out = [];
        GmailApp.search('from:(mailer-daemon OR postmaster) newer_than:3d', 0, 25).forEach(function (t) {
          t.getMessages().forEach(function (m) {
            out.push({ id: m.getId(), from: m.getFrom(), subject: m.getSubject(), body: String(m.getPlainBody()).slice(0, 6000) });
          });
        });
        return out;
      },
    },
    alert: function (subject, body) {
      const to = cfg().ALERT_EMAIL;
      if (to) MailApp.sendEmail(to, subject, body);
    },
    log: function (event, detail) { logSheet.appendRow([new Date().toISOString(), event, String(detail).slice(0, 500)]); },
  };
}
