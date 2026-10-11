/**
 * Engine.gs: the sender's orchestration. Takes a `d` (dependencies) object so it can be tested in
 * Node with fakes, and run in Apps Script with the real adapters (Adapters.gs).
 *
 * d.config.load() / d.config.set(k, v)      Config tab
 * d.now()  d.parts(date)  d.rng()           clock (parts = {date, minutes, iso} in the configured timezone)
 * d.props.get(k) / d.props.set(k, v)        small persistent state
 * d.queue.rows() / update(row, fields) / append(rows)
 * d.suppress.has(email) / add(email, reason)
 * d.gmail.myEmail() sendFirst(row,cfg) sendFollowup(row,cfg) threadMessages(threadId) searchBounces()
 * d.alert(subject, body)  d.log(event, detail)
 *
 * SAFETY RULES enforced here:
 *  - write-ahead: a row is marked 'sending' BEFORE the email goes out, so a crash can never cause a
 *    second send. A row stuck in 'sending' needs a human look; it is never retried automatically.
 *  - no automatic retry after a send error (a failed send might still have gone out).
 *  - exactly one follow-up per person, never late, never after a reply or bounce.
 */

const TICK_MINUTES = 10;   // must match installTrigger() in Setup.gs
const SENT_LIKE = { sent: true, replied: true, bounced: true, suppressed: true, sending: true };

function isTrue(v) { return String(v).toUpperCase() === 'TRUE'; }

function bounceTypeOf(row) {
  if (!isTrue(row.bounce_flag)) return '';
  const m = /^bounce:(\w+)/.exec(row.error || '');
  return m ? m[1] : 'hard';
}

// ---------------------------------------------------------------- tick

function runTick(d) {
  const cfg = d.config.load();
  const parts = d.parts(d.now());
  if (cfg.PAUSED) return { action: 'paused' };

  detectBounces(d, cfg, parts);
  detectReplies(d, cfg, parts);
  expireFollowups(d, cfg, parts);

  const ks = evaluateKill(d, cfg, parts);
  if (ks.pause) {
    pauseSending(d, ks.reason, parts);
    return { action: 'killed', reason: ks.reason };
  }

  if (!isSendDay(parts) || !inWindow(parts, cfg)) return { action: 'outside_window' };
  const cap = effectiveCap(cfg, parts.date);
  if (cap <= 0) return { action: 'cap_zero' };

  const plan = getPlan(d, cfg, parts, cap);
  let rows = d.queue.rows();
  const sentToday = countSentToday(rows, parts.date, cfg);
  const allowed = sendsAllowedNow(plan.slots, sentToday, cap, parts.minutes, cfg.MAX_PER_RUN);
  const result = { action: 'ran', cap: cap, sentToday: sentToday, allowed: allowed, sent: 0, followups: 0 };

  for (let i = 0; i < allowed; i++) {
    rows = d.queue.rows();
    const due = rows.filter(function (r) { return followupState(r, parts.date, cfg) === 'eligible'; })
      .sort(function (a, b) { return a.followup_due < b.followup_due ? -1 : 1; });
    let ok;
    if (due.length) {
      ok = doFollowup(d, cfg, parts, due[0]);
      if (ok === true) result.followups++;
    } else {
      const next = nextQueued(d, rows, parts);
      if (!next) break;
      ok = doFirst(d, cfg, parts, next);
      if (ok === true) result.sent++;
    }
    if (ok === 'unrecorded') { result.action = 'killed'; result.reason = 'bookkeeping failure after a send'; break; }
    const again = evaluateKill(d, cfg, parts);
    if (again.pause) {
      pauseSending(d, again.reason, parts);
      result.action = 'killed';
      result.reason = again.reason;
      break;
    }
  }
  return result;
}

function countSentToday(rows, dateStr, cfg) {
  let n = 0;
  rows.forEach(function (r) {
    if (String(r.sent_at || '').indexOf(dateStr) === 0) n++;
    else if (r.status === 'sending' && String(r.scheduled_at || '').indexOf(dateStr) === 0) n++;
    if (cfg.FOLLOWUPS_COUNT_AGAINST_CAP && String(r.followup_sent_at || '').indexOf(dateStr) === 0) n++;
  });
  return n;
}

/** One plan per day: random send minutes over what is left of the window. Also stamps planned times. */
function getPlan(d, cfg, parts, cap) {
  let plan = null;
  try { plan = JSON.parse(d.props.get('plan') || 'null'); } catch (e) { plan = null; }
  if (plan && plan.date === parts.date) return plan;
  const startMin = Math.max(parseHM(cfg.SEND_START), parts.minutes);
  // Stop one tick short of the end: a slot in the final tick interval could never fire.
  const slots = planSlots(cap, startMin, parseHM(cfg.SEND_END) - TICK_MINUTES, cfg.MIN_GAP_MINUTES, d.rng);
  plan = { date: parts.date, cap: cap, slots: slots };
  d.props.set('plan', JSON.stringify(plan));
  const queued = d.queue.rows().filter(function (r) { return r.status === 'queued'; })
    .sort(function (a, b) { return String(a.queued_at) < String(b.queued_at) ? -1 : 1; });
  slots.forEach(function (m, i) {
    if (!queued[i]) return;
    const hh = ('0' + Math.floor(m / 60)).slice(-2), mm = ('0' + (m % 60)).slice(-2);
    d.queue.update(queued[i], { scheduled_at: parts.date + 'T' + hh + ':' + mm });
  });
  return plan;
}

function nextQueued(d, rows, parts) {
  const queued = rows.filter(function (r) { return r.status === 'queued'; })
    .sort(function (a, b) { return String(a.queued_at) < String(b.queued_at) ? -1 : 1; });
  for (let i = 0; i < queued.length; i++) {
    const r = queued[i];
    const email = String(r.email || '').trim().toLowerCase();
    if (r.not_before && parts.date < r.not_before) continue;
    if (!isValidEmail(email) || !r.subject || !r.body) {
      d.queue.update(r, { status: 'error', error: 'invalid row: missing or malformed email, subject or body' });
      continue;
    }
    if (d.suppress.has(email)) {
      d.queue.update(r, { status: 'suppressed', error: 'on suppression list' });
      continue;
    }
    const twin = rows.some(function (o) {
      return o !== r && String(o.email).toLowerCase() === email && SENT_LIKE[o.status];
    });
    if (twin) {
      d.queue.update(r, { status: 'suppressed', error: 'duplicate: this address was already sent to' });
      continue;
    }
    return r;
  }
  return null;
}

function doFirst(d, cfg, parts, row) {
  d.queue.update(row, { status: 'sending', scheduled_at: row.scheduled_at || parts.iso });   // write-ahead
  let res;
  try {
    res = d.gmail.sendFirst(row, cfg);
  } catch (e) {
    d.queue.update(row, { status: 'error', error: 'send failed: ' + String(e && e.message || e).slice(0, 180) });
    d.props.set('consec_errors', String(Number(d.props.get('consec_errors') || 0) + 1));
    d.log('SEND_ERROR', row.id + ' ' + e);
    return false;
  }
  // The email HAS gone out. From here a failure is a bookkeeping failure, never a send failure.
  try {
    const nowParts = d.parts(d.now());
    const due = pickFollowupDate(nowParts.date, cfg.FOLLOWUP_MIN_DAYS, cfg.FOLLOWUP_MAX_DAYS, d.rng);
    d.queue.update(row, {
      status: 'sent', sent_at: nowParts.iso, thread_id: res.threadId, message_id: res.messageId,
      followup_due: due, followup_status: 'pending', error: '',
    });
    d.props.set('consec_errors', '0');
    d.log('SENT', row.id);
    return true;
  } catch (e) {
    d.log('RECORD_FAILED_AFTER_SEND', row.id + ' thread ' + res.threadId + ' ' + e);
    pauseSending(d, 'an email to row ' + row.id + ' was sent but the sheet could not be updated (row left in "sending", thread ' +
      res.threadId + '). Fix the sheet, then set that row by hand before resuming', d.parts(d.now()));
    return 'unrecorded';
  }
}

function doFollowup(d, cfg, parts, row) {
  // Last look for a reply before nudging anyone.
  const me = String(d.gmail.myEmail() || '').toLowerCase();
  try {
    const msgs = d.gmail.threadMessages(row.thread_id);
    const human = msgs.filter(function (m) { return String(m.from).toLowerCase().indexOf(me) < 0; })
      .map(classifyMessage);
    if (human.indexOf('reply') >= 0 || human.indexOf('opt_out') >= 0) {
      d.queue.update(row, { reply_flag: 'TRUE', status: human.indexOf('opt_out') >= 0 ? 'suppressed' : 'replied',
        followup_status: 'skipped_reply', checked_at: parts.iso });
      if (human.indexOf('opt_out') >= 0) { d.suppress.add(row.email, 'opt_out'); d.queue.update(row, { error: 'opt_out' }); }
      return 'skipped';
    }
  } catch (e) {
    d.log('FOLLOWUP_PRECHECK_ERROR', row.id + ' ' + e);
    return false;            // cannot confirm no reply: do not send
  }
  d.queue.update(row, { followup_status: 'sending' });        // write-ahead
  try {
    d.gmail.sendFollowup(row, cfg);
  } catch (e) {
    d.queue.update(row, { followup_status: 'error', error: 'follow-up failed: ' + String(e && e.message || e).slice(0, 170) });
    d.props.set('consec_errors', String(Number(d.props.get('consec_errors') || 0) + 1));
    d.log('FOLLOWUP_ERROR', row.id + ' ' + e);
    return false;
  }
  try {
    d.queue.update(row, { followup_status: 'sent', followup_sent_at: d.parts(d.now()).iso, error: '' });
    d.props.set('consec_errors', '0');
    d.log('FOLLOWUP_SENT', row.id);
    return true;
  } catch (e) {
    d.log('RECORD_FAILED_AFTER_FOLLOWUP', row.id + ' ' + e);
    pauseSending(d, 'a follow-up to row ' + row.id + ' was sent but the sheet could not be updated (left in "sending")', d.parts(d.now()));
    return 'unrecorded';
  }
}

function expireFollowups(d, cfg, parts) {
  d.queue.rows().forEach(function (r) {
    if (followupState(r, parts.date, cfg) === 'expired') d.queue.update(r, { followup_status: 'expired' });
  });
}

// ---------------------------------------------------------------- detection

function detectReplies(d, cfg, parts) {
  const cutoff = addDays(parts.date, -cfg.REPLY_LOOKBACK_DAYS);
  const me = String(d.gmail.myEmail() || '').toLowerCase();
  const candidates = d.queue.rows().filter(function (r) {
    return r.status === 'sent' && r.thread_id && !isTrue(r.reply_flag) && String(r.sent_at).slice(0, 10) >= cutoff;
  }).sort(function (a, b) { return String(a.checked_at) < String(b.checked_at) ? -1 : 1; })
    .slice(0, cfg.REPLY_CHECKS_PER_RUN);
  let found = 0;
  candidates.forEach(function (r) {
    let msgs;
    try { msgs = d.gmail.threadMessages(r.thread_id); } catch (e) { d.log('THREAD_READ_ERROR', r.id + ' ' + e); return; }
    const theirs = msgs.filter(function (m) { return String(m.from).toLowerCase().indexOf(me) < 0; });
    const kinds = theirs.map(classifyMessage);
    if (kinds.indexOf('opt_out') >= 0) {
      d.queue.update(r, { reply_flag: 'TRUE', status: 'suppressed', followup_status: 'skipped_reply', error: 'opt_out', checked_at: parts.iso });
      d.suppress.add(r.email, 'opt_out');
      found++;
    } else if (kinds.indexOf('reply') >= 0) {
      d.queue.update(r, { reply_flag: 'TRUE', status: 'replied', followup_status: 'skipped_reply', checked_at: parts.iso });
      found++;
    } else {
      const bounce = kinds.filter(function (k) { return k.indexOf('bounce_') === 0; })[0];
      if (bounce) {
        d.queue.update(r, { bounce_flag: 'TRUE', status: 'bounced', followup_status: 'skipped_bounce', error: bounce.replace('_', ':'), checked_at: parts.iso });
        d.suppress.add(r.email, bounce);
      } else {
        d.queue.update(r, { checked_at: parts.iso });
      }
    }
  });
  return found;
}

function detectBounces(d, cfg, parts) {
  const cutoff = addDays(parts.date, -14);
  const byEmail = {};
  d.queue.rows().forEach(function (r) {
    if (r.status === 'sent' && String(r.sent_at).slice(0, 10) >= cutoff && !isTrue(r.bounce_flag)) byEmail[String(r.email).toLowerCase()] = r;
  });
  const known = new Set(Object.keys(byEmail));
  if (!known.size) return 0;
  let seen = [];
  try { seen = JSON.parse(d.props.get('bounce_seen') || '[]'); } catch (e) { seen = []; }
  let marked = 0;
  d.gmail.searchBounces().forEach(function (m) {
    if (seen.indexOf(m.id) >= 0) return;
    seen.push(m.id);
    const text = (m.subject || '') + '\n' + (m.body || '');
    const kind = classifyBounce(text);
    extractKnownAddresses(text, known).forEach(function (addr) {
      const r = byEmail[addr];
      if (!r || isTrue(r.bounce_flag)) return;
      d.queue.update(r, { bounce_flag: 'TRUE', status: 'bounced', followup_status: 'skipped_bounce', error: 'bounce:' + kind, checked_at: parts.iso });
      d.suppress.add(addr, 'bounce:' + kind);
      marked++;
    });
  });
  d.props.set('bounce_seen', JSON.stringify(seen.slice(-500)));
  return marked;
}

// ---------------------------------------------------------------- kill switch

function evaluateKill(d, cfg, parts) {
  const rows = d.queue.rows();
  const sends = rows.filter(function (r) { return r.sent_at; })
    .sort(function (a, b) { return a.sent_at < b.sent_at ? -1 : 1; })
    .map(function (r) { return { bounce_type: bounceTypeOf(r) }; });
  const cut30 = addDays(parts.date, -30);
  const ctx = {
    bouncesToday: rows.filter(function (r) {
      const t = bounceTypeOf(r);
      return (t === 'hard' || t === 'block') && String(r.checked_at || '').indexOf(parts.date) === 0;
    }).length,
    optOuts: rows.filter(function (r) { return r.error === 'opt_out' && String(r.checked_at || '').slice(0, 10) >= cut30; }).length,
    consecutiveErrors: Number(d.props.get('consec_errors') || 0),
  };
  return killSwitch(sends, cfg, ctx);
}

function pauseSending(d, reason, parts) {
  d.config.set('PAUSED', 'TRUE');
  d.config.set('PAUSE_REASON', reason + ' (paused ' + parts.iso + ')');
  d.log('KILL_SWITCH', reason);
  d.alert('Outreach sender PAUSED', 'Sending stopped automatically.\n\nReason: ' + reason +
    '\n\nNothing more will send until you fix the cause and choose Outreach > Resume sending in the sheet.');
}

// ---------------------------------------------------------------- bridge (web app)

function safeEqual(a, b) {
  a = String(a || ''); b = String(b || '');
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function handleBridge(d, req, expectedToken) {
  if (!expectedToken || !safeEqual(req && req.token, expectedToken)) return { ok: false, error: 'unauthorized' };
  const rows = d.queue.rows();
  if (req.action === 'append') {
    const have = {};
    rows.forEach(function (r) { have[String(r.email).toLowerCase()] = true; });
    const skipped = [];
    const toAdd = [];
    (req.rows || []).forEach(function (r) {
      const email = String(r.email || '').trim().toLowerCase();
      if (!isValidEmail(email)) { skipped.push({ email: email, reason: 'invalid email' }); return; }
      if (!r.subject || !r.body) { skipped.push({ email: email, reason: 'missing subject or body' }); return; }
      if (have[email]) { skipped.push({ email: email, reason: 'already in sheet' }); return; }
      if (d.suppress.has(email)) { skipped.push({ email: email, reason: 'on suppression list' }); return; }
      have[email] = true;
      toAdd.push(Object.assign({}, r, { email: email, status: 'queued' }));
    });
    if (toAdd.length) d.queue.append(toAdd);
    return { ok: true, added: toAdd.length, skipped: skipped };
  }
  if (req.action === 'export') {
    return { ok: true, rows: rows.map(function (r) {
      return { id: r.id, email: r.email, name: r.name, firm: r.firm, status: r.status, sent_at: r.sent_at,
        followup_due: r.followup_due, followup_status: r.followup_status, followup_sent_at: r.followup_sent_at,
        reply_flag: r.reply_flag, bounce_flag: r.bounce_flag };
    }) };
  }
  if (req.action === 'suppress') {
    (req.emails || []).forEach(function (e) { d.suppress.add(String(e).toLowerCase(), req.reason || 'manual'); });
    return { ok: true, suppressed: (req.emails || []).length };
  }
  if (req.action === 'status') {
    const counts = {};
    rows.forEach(function (r) { counts[r.status] = (counts[r.status] || 0) + 1; });
    return { ok: true, counts: counts };
  }
  return { ok: false, error: 'unknown action' };
}
