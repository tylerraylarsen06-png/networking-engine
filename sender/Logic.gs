/**
 * Logic.gs: pure functions only. No Apps Script globals (no SpreadsheetApp, GmailApp, Utilities).
 * Everything here is unit-tested in Node: node sender/test/run_tests.js
 *
 * Dates are passed around as plain strings so timezone handling stays in one place (Adapters.gs):
 *   dateStr  'YYYY-MM-DD'   in the script's configured timezone
 *   minutes  minutes since local midnight
 */

// Keep in sync with SHEET_COLUMNS in bridge.py (test_bridge.py checks this).
const COLUMNS = [
  'id', 'email', 'name', 'firm', 'subject', 'body', 'followup_body', 'status',
  'not_before', 'scheduled_at', 'sent_at', 'thread_id', 'message_id',
  'followup_due', 'followup_status', 'followup_sent_at', 'reply_flag',
  'bounce_flag', 'error', 'queued_at', 'checked_at',
];

// Every setting lives on the Config tab. These are the defaults Setup writes there.
// The whole install starts PAUSED and with the ramp anchored to a blank start date,
// so it sends nothing until you set WARMUP_START_DATE and resume.
const CONFIG_DEFAULTS = {
  PAUSED: 'TRUE',
  PAUSE_REASON: 'new install: run the self test, set WARMUP_START_DATE, then Outreach > Resume sending',
  DAILY_CAP: 20,                       // hard ceiling. The number you edit by hand.
  RAMP: '1:5,2:8,3:12,4:16,5:20',      // week-of-warmup:cap. Effective cap = min(DAILY_CAP, ramp).
  WARMUP_START_DATE: '',               // YYYY-MM-DD of the first cold send day. Blank = cap 0.
  TIMEZONE: 'America/Los_Angeles',
  SEND_START: '09:00',
  SEND_END: '16:30',
  MIN_GAP_MINUTES: 12,                 // minimum spacing between sends
  MAX_PER_RUN: 1,                      // never more than this many sends in one 10 minute tick
  FOLLOWUP_MIN_DAYS: 6,
  FOLLOWUP_MAX_DAYS: 8,
  FOLLOWUP_GRACE_DAYS: 0,              // 0 = a follow-up that misses day 8 is dropped, never sent late
  FOLLOWUPS_COUNT_AGAINST_CAP: 'TRUE',
  KILL_WINDOW_SENDS: 20,               // look at the last N first-sends
  KILL_MIN_SAMPLE: 15,                 // need at least this many sends before the rate counts
  KILL_BOUNCE_RATE: 0.10,              // hard bounces / window >= this pauses sending (2 of the last 20)
  KILL_MAX_BOUNCES_PER_DAY: 3,
  KILL_MAX_BLOCKS: 2,                  // reputation/policy rejections (5.7.x) across the window
  KILL_MAX_OPTOUTS: 2,                 // replies asking to stop, last 30 days
  KILL_MAX_ERRORS: 3,                  // consecutive send failures
  SENDER_NAME: '',                     // your name as it appears in the From header
  ALERT_EMAIL: '',                     // personal address that gets the pause alert
  TEST_ADDRESS: '',                    // where the self test sends
  REPLY_LOOKBACK_DAYS: 30,
  REPLY_CHECKS_PER_RUN: 25,
};

const NUMERIC_KEYS = [
  'DAILY_CAP', 'MIN_GAP_MINUTES', 'MAX_PER_RUN', 'FOLLOWUP_MIN_DAYS', 'FOLLOWUP_MAX_DAYS',
  'FOLLOWUP_GRACE_DAYS', 'KILL_WINDOW_SENDS', 'KILL_MIN_SAMPLE', 'KILL_BOUNCE_RATE',
  'KILL_MAX_BOUNCES_PER_DAY', 'KILL_MAX_BLOCKS', 'KILL_MAX_OPTOUTS', 'KILL_MAX_ERRORS',
  'REPLY_LOOKBACK_DAYS', 'REPLY_CHECKS_PER_RUN',
];
const BOOLEAN_KEYS = ['PAUSED', 'FOLLOWUPS_COUNT_AGAINST_CAP'];

function toBool(v) {
  return String(v).trim().toUpperCase() === 'TRUE';
}

/** raw: {KEY: value as read from the Config tab (strings)}. Returns typed config with defaults filled. */
function mergeConfig(raw) {
  const cfg = {};
  Object.keys(CONFIG_DEFAULTS).forEach(function (k) {
    let v = raw && raw[k] !== undefined && raw[k] !== '' ? raw[k] : CONFIG_DEFAULTS[k];
    if (raw && raw[k] === '' && (k === 'WARMUP_START_DATE' || k === 'ALERT_EMAIL' || k === 'TEST_ADDRESS' || k === 'RAMP')) v = '';
    if (NUMERIC_KEYS.indexOf(k) >= 0) {
      const n = Number(v);
      v = isFinite(n) ? n : Number(CONFIG_DEFAULTS[k]);
    } else if (BOOLEAN_KEYS.indexOf(k) >= 0) {
      v = toBool(v);
    } else {
      v = String(v).trim();
    }
    cfg[k] = v;
  });
  return cfg;
}

function parseHM(s) {
  const m = /^(\d{1,2}):(\d{2})$/.exec(String(s).trim());
  if (!m) throw new Error('bad time ' + s + ' (want HH:MM)');
  return Number(m[1]) * 60 + Number(m[2]);
}

// ---------- date helpers (string based, no timezone surprises) ----------

function dateToUTC(dateStr) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(dateStr);
  if (!m) throw new Error('bad date ' + dateStr);
  return Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
}
function dayOfWeek(dateStr) { return new Date(dateToUTC(dateStr)).getUTCDay(); } // 0 Sun .. 6 Sat
function addDays(dateStr, n) {
  const d = new Date(dateToUTC(dateStr) + n * 86400000);
  return d.toISOString().slice(0, 10);
}
function daysBetween(fromStr, toStr) {
  return Math.round((dateToUTC(toStr) - dateToUTC(fromStr)) / 86400000);
}
function isWeekday(dateStr) { const d = dayOfWeek(dateStr); return d >= 1 && d <= 5; }

// ---------- the cap: one place, ramped ----------

function parseRamp(spec) {
  if (!spec) return [];
  return String(spec).split(',').map(function (p) {
    const m = /^\s*(\d+)\s*:\s*(\d+)\s*$/.exec(p);
    if (!m) throw new Error('bad RAMP entry "' + p + '" (want week:cap, e.g. 1:5)');
    return { week: Number(m[1]), cap: Number(m[2]) };
  }).sort(function (a, b) { return a.week - b.week; });
}

/** Cap from the ramp table alone. Infinity when RAMP is empty. 0 before/without a start date. */
function rampCap(cfg, todayStr) {
  const ramp = parseRamp(cfg.RAMP);
  if (!ramp.length) return Infinity;
  if (!cfg.WARMUP_START_DATE) return 0;
  const days = daysBetween(cfg.WARMUP_START_DATE, todayStr);
  if (days < 0) return 0;
  const week = Math.floor(days / 7) + 1;
  let cap = 0;
  ramp.forEach(function (r) { if (r.week <= week) cap = r.cap; });
  return cap;
}

/** The single number the sender obeys today. */
function effectiveCap(cfg, todayStr) {
  return Math.max(0, Math.min(cfg.DAILY_CAP, rampCap(cfg, todayStr)));
}

// ---------- spreading sends across the day ----------

function isSendDay(parts) { return isWeekday(parts.date); }
function inWindow(parts, cfg) {
  return parts.minutes >= parseHM(cfg.SEND_START) && parts.minutes < parseHM(cfg.SEND_END);
}

/**
 * Random send minutes across [startMin, endMin) with a guaranteed minimum gap.
 * Not a burst and not evenly spaced. If the gap is infeasible for the cap it shrinks.
 */
function planSlots(cap, startMin, endMin, gap, rng) {
  if (cap <= 0 || endMin <= startMin) return [];
  const span = endMin - startMin;
  let g = gap;
  if ((cap - 1) * g > span - 1) g = Math.max(1, Math.floor((span - 1) / Math.max(1, cap - 1)));
  const free = Math.max(0, span - 1 - (cap - 1) * g);
  const offsets = [];
  for (let i = 0; i < cap; i++) offsets.push(Math.floor(rng() * (free + 1)));
  offsets.sort(function (a, b) { return a - b; });
  return offsets.map(function (o, i) { return startMin + o + i * g; });
}

/** How many sends this tick may make. Never above the cap, the plan, or MAX_PER_RUN. */
function sendsAllowedNow(plan, sentToday, cap, nowMin, maxPerRun) {
  const due = plan.filter(function (m) { return m <= nowMin; }).length;
  return Math.max(0, Math.min(due - sentToday, cap - sentToday, maxPerRun));
}

// ---------- the single follow-up ----------

/** A weekday 6 to 8 days after the send date (both ends inclusive). Random among weekdays. */
function pickFollowupDate(sentDateStr, minDays, maxDays, rng) {
  const options = [];
  for (let d = minDays; d <= maxDays; d++) {
    const cand = addDays(sentDateStr, d);
    if (isWeekday(cand)) options.push(cand);
  }
  if (!options.length) {
    let d = maxDays;
    while (!isWeekday(addDays(sentDateStr, d))) d++;
    return addDays(sentDateStr, d);
  }
  return options[Math.min(options.length - 1, Math.floor(rng() * options.length))];
}

/**
 * 'eligible' | 'not_due' | 'expired' | 'skip' for one row.
 *  skip     = follow-up already sent, or no follow-up applies (reply, bounce, suppressed, not sent)
 *  expired  = due date passed by more than the grace window; NEVER sent late
 * There is exactly one follow-up. followup_sent_at set means this always returns 'skip'.
 */
function followupState(row, todayStr, cfg) {
  if (row.status !== 'sent') return 'skip';
  if (row.followup_sent_at || row.followup_status === 'sent') return 'skip';
  if (row.followup_status === 'sending' || row.followup_status === 'error') return 'skip';   // never retry an ambiguous follow-up
  if (String(row.reply_flag).toUpperCase() === 'TRUE' || String(row.bounce_flag).toUpperCase() === 'TRUE') return 'skip';
  if (!row.followup_due || !row.followup_body || !row.thread_id || !row.message_id) return 'skip';
  if (row.followup_status === 'expired') return 'skip';
  if (todayStr < row.followup_due) return 'not_due';
  const lastDay = addDays(String(row.sent_at).slice(0, 10), cfg.FOLLOWUP_MAX_DAYS + cfg.FOLLOWUP_GRACE_DAYS);
  if (todayStr > lastDay) return 'expired';
  return 'eligible';
}

// ---------- reading replies and bounces ----------

const AUTO_REPLY_RE = /out of (the )?office|automatic reply|auto-?reply|autoreply|on (annual )?leave|away (from (my )?email )?until|vacation responder|i am currently out/i;
const OPT_OUT_RE = /\b(unsubscribe|remove me|stop (emailing|contacting|sending|messaging)|do not (contact|email)|don'?t (contact|email)|take me off|cease (contact|emailing)|report(ed)? (you|this)|this is spam)\b/i;
const BOUNCE_SENDER_RE = /mailer-daemon|postmaster|mail delivery (sub)?system/i;

function classifyBounce(text) {
  const t = String(text || '');
  if (/mailbox full|over quota|quota exceeded|temporar|try again|deferred|4\.\d\.\d/i.test(t)) return 'soft';
  if (/\b5\.7\.\d+\b|blocked|spam|reputation|dmarc|policy|rejected due to|not authorized|unauthenticated/i.test(t)) return 'block';
  if (/\b5\.\d\.\d+\b|\b55[0-9]\b|does not exist|address not found|couldn'?t be found|no such user|user unknown|unknown recipient|invalid recipient|unable to receive mail/i.test(t)) return 'hard';
  return 'soft';
}

/**
 * m: {from, subject, body, autoSubmitted}
 * -> 'bounce_hard' | 'bounce_block' | 'bounce_soft' | 'auto_reply' | 'opt_out' | 'reply'
 */
function classifyMessage(m) {
  if (BOUNCE_SENDER_RE.test(m.from || '')) return 'bounce_' + classifyBounce((m.subject || '') + ' ' + (m.body || ''));
  const auto = m.autoSubmitted && String(m.autoSubmitted).toLowerCase() !== 'no';
  if (auto || AUTO_REPLY_RE.test((m.subject || '') + ' ' + String(m.body || '').slice(0, 600))) return 'auto_reply';
  if (OPT_OUT_RE.test(m.body || '')) return 'opt_out';
  return 'reply';
}

/** Which of the emails we sent to appear in a bounce message. known: Set of lowercase addresses. */
function extractKnownAddresses(text, known) {
  const found = {};
  (String(text || '').toLowerCase().match(/[a-z0-9._%+\-']+@[a-z0-9.\-]+\.[a-z]{2,}/g) || []).forEach(function (a) {
    if (known.has(a)) found[a] = true;
  });
  return Object.keys(found);
}

// ---------- the kill switch ----------

/**
 * sends: first-sends in send order, each {bounce_type: '' | 'hard' | 'block' | 'soft'}
 * ctx:   {bouncesToday, optOuts, consecutiveErrors}
 * Returns {pause: boolean, reason: string}. Soft bounces never count.
 */
function killSwitch(sends, cfg, ctx) {
  const recent = sends.slice(-cfg.KILL_WINDOW_SENDS);
  const hard = recent.filter(function (s) { return s.bounce_type === 'hard' || s.bounce_type === 'block'; }).length;
  const blocks = recent.filter(function (s) { return s.bounce_type === 'block'; }).length;
  if (blocks >= cfg.KILL_MAX_BLOCKS) return { pause: true, reason: blocks + ' reputation/policy rejections (5.7.x) in the last ' + recent.length + ' sends' };
  if (ctx.bouncesToday >= cfg.KILL_MAX_BOUNCES_PER_DAY) return { pause: true, reason: ctx.bouncesToday + ' hard bounces today' };
  if (recent.length >= cfg.KILL_MIN_SAMPLE && hard / recent.length >= cfg.KILL_BOUNCE_RATE) {
    return { pause: true, reason: 'bounce rate ' + Math.round(1000 * hard / recent.length) / 10 + '% (' + hard + ' of last ' + recent.length + ') is at or above ' + cfg.KILL_BOUNCE_RATE * 100 + '%' };
  }
  if (ctx.optOuts >= cfg.KILL_MAX_OPTOUTS) return { pause: true, reason: ctx.optOuts + ' opt-out or spam replies in the last 30 days' };
  if (ctx.consecutiveErrors >= cfg.KILL_MAX_ERRORS) return { pause: true, reason: ctx.consecutiveErrors + ' send failures in a row' };
  return { pause: false, reason: '' };
}

// ---------- building the threaded follow-up ----------

function stripCRLF(s) { return String(s == null ? '' : s).replace(/[\r\n]+/g, ' ').trim(); }

/** RFC 2047 encode a header value when it has non-ASCII characters. b64: string -> base64 of its UTF-8 bytes. */
function mimeHeaderEncode(text, b64) {
  const clean = stripCRLF(text);
  return /^[\x20-\x7e]*$/.test(clean) ? clean : '=?UTF-8?B?' + b64(clean) + '?=';
}

/**
 * Raw RFC 2822 message for a reply that Gmail will file in the original thread.
 * Threading needs the matching thread id (passed to the API separately), a matching
 * subject, and In-Reply-To / References pointing at the original Message-ID.
 */
function buildReplyMime(o, b64) {
  const lines = [
    'To: ' + stripCRLF(o.to),
    'Subject: ' + mimeHeaderEncode(o.subject, b64),
    'In-Reply-To: ' + stripCRLF(o.inReplyTo),
    'References: ' + stripCRLF(o.references || o.inReplyTo),
    'MIME-Version: 1.0',
    'Content-Type: text/plain; charset=UTF-8',
    'Content-Transfer-Encoding: 8bit',
  ];
  if (o.from) lines.unshift('From: ' + stripCRLF(o.from));
  return lines.join('\r\n') + '\r\n\r\n' + String(o.body);
}

function isValidEmail(s) {
  return /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(String(s || '').trim());
}
