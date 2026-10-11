/**
 * Setup.gs: one-time setup, the menu, and the trigger entry point.
 * The install starts PAUSED. It cannot send until you set WARMUP_START_DATE and choose Resume.
 */

function tick() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(20000)) return;
  try {
    runTick(makeDeps());
  } catch (e) {
    try { makeDeps().log('TICK_ERROR', e); } catch (ignore) { /* logging must never throw */ }
  } finally {
    lock.releaseLock();
  }
}

function onOpen() {
  SpreadsheetApp.getUi().createMenu('Outreach')
    .addItem('1. Set up sheet', 'setupSheet')
    .addItem('2. Show bridge token (once)', 'showBridgeToken')
    .addItem('3. Install 10-minute trigger', 'installTrigger')
    .addItem('4. Self test (sends 2 emails to TEST_ADDRESS)', 'selfTest')
    .addSeparator()
    .addItem('Status', 'statusReport')
    .addItem('Pause sending', 'pauseNow')
    .addItem('Resume sending', 'resumeNow')
    .addToUi();
}

function setupSheet() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  function tab(name) { return ss.getSheetByName(name) || ss.insertSheet(name); }

  const q = tab(TAB_QUEUE);
  q.getRange(1, 1, 1, COLUMNS.length).setValues([COLUMNS]).setFontWeight('bold');
  q.getRange(1, 1, q.getMaxRows(), COLUMNS.length).setNumberFormat('@');   // plain text: no date auto-conversion
  q.setFrozenRows(1);

  const c = tab(TAB_CONFIG);
  if (c.getLastRow() < 2) {
    const rows = [['key', 'value']].concat(Object.keys(CONFIG_DEFAULTS).map(function (k) { return [k, CONFIG_DEFAULTS[k]]; }));
    c.getRange(1, 1, rows.length, 2).setValues(rows);
    c.getRange(1, 1, 1, 2).setFontWeight('bold');
  }
  c.getRange(1, 2, c.getMaxRows(), 1).setNumberFormat('@');

  const s = tab(TAB_SUPPRESS);
  if (s.getLastRow() < 1) s.getRange(1, 1, 1, 3).setValues([['email', 'reason', 'added_at']]).setFontWeight('bold');
  const l = tab(TAB_LOG);
  if (l.getLastRow() < 1) l.getRange(1, 1, 1, 3).setValues([['time', 'event', 'detail']]).setFontWeight('bold');

  if (!PropertiesService.getScriptProperties().getProperty('BRIDGE_TOKEN')) {
    PropertiesService.getScriptProperties().setProperty('BRIDGE_TOKEN', Utilities.getUuid() + Utilities.getUuid().replace(/-/g, ''));
  }
  SpreadsheetApp.getUi().alert('Sheet is set up and PAUSED.\n\nNext: fill ALERT_EMAIL and TEST_ADDRESS on the Config tab, then run the self test.');
}

function showBridgeToken() {
  const t = PropertiesService.getScriptProperties().getProperty('BRIDGE_TOKEN');
  SpreadsheetApp.getUi().alert('SHEET_WEBAPP_TOKEN (copy into the .env file next to find.py, never into a chat):\n\n' + t);
}

function installTrigger() {
  ScriptApp.getProjectTriggers().forEach(function (t) { if (t.getHandlerFunction() === 'tick') ScriptApp.deleteTrigger(t); });
  ScriptApp.newTrigger('tick').timeBased().everyMinutes(10).create();
  SpreadsheetApp.getUi().alert('Installed: tick() runs every 10 minutes. It only sends Monday to Friday inside the send window, and only while not paused.');
}

function pauseNow() {
  const d = makeDeps();
  d.config.set('PAUSED', 'TRUE');
  d.config.set('PAUSE_REASON', 'paused by hand ' + d.parts(new Date()).iso);
  SpreadsheetApp.getUi().alert('Sending paused.');
}

function resumeNow() {
  const d = makeDeps();
  const cfg = d.config.load();
  const cap = effectiveCap(cfg, d.parts(new Date()).date);
  const ui = SpreadsheetApp.getUi();
  const ok = ui.alert('Resume sending?',
    'Last pause reason: ' + cfg.PAUSE_REASON + '\n\nToday\'s effective cap would be ' + cap + '. Resume only if you have fixed the cause.',
    ui.ButtonSet.YES_NO);
  if (ok === ui.Button.YES) {
    d.props.set('consec_errors', '0');
    d.config.set('PAUSED', 'FALSE');
    d.config.set('PAUSE_REASON', '');
    ui.alert('Resumed.');
  }
}

function statusReport() {
  const d = makeDeps();
  const cfg = d.config.load();
  const parts = d.parts(new Date());
  const rows = d.queue.rows();
  const counts = {};
  rows.forEach(function (r) { counts[r.status] = (counts[r.status] || 0) + 1; });
  const stuck = rows.filter(function (r) { return r.status === 'sending' || r.followup_status === 'sending'; }).length;
  const ev = evaluateKill(d, cfg, parts);
  SpreadsheetApp.getUi().alert(
    'Paused: ' + cfg.PAUSED + (cfg.PAUSED ? '\nReason: ' + cfg.PAUSE_REASON : '') +
    '\nEffective cap today: ' + effectiveCap(cfg, parts.date) + ' (DAILY_CAP ' + cfg.DAILY_CAP + ', ramp ' + (isFinite(rampCap(cfg, parts.date)) ? rampCap(cfg, parts.date) : 'off') + ')' +
    '\nQueue: ' + JSON.stringify(counts) +
    '\nRows stuck in "sending" (need a human look): ' + stuck +
    '\nKill switch would trip now: ' + ev.pause + (ev.reason ? ' (' + ev.reason + ')' : ''));
}

/**
 * Sends one email and one in-thread follow-up to TEST_ADDRESS so you can check, by eye, that:
 *  1. both land in ONE thread, and
 *  2. the headers show spf=pass, dkim=pass, dmarc=pass ("Show original" in Gmail).
 */
function selfTest() {
  const d = makeDeps();
  const cfg = d.config.load();
  if (!isValidEmail(cfg.TEST_ADDRESS)) throw new Error('Set TEST_ADDRESS on the Config tab first (an inbox you can read, not this one).');
  const row = {
    id: 'selftest', email: cfg.TEST_ADDRESS, subject: '[test] outreach sender self test ' + d.parts(new Date()).iso,
    body: 'Hi,\n\nThis is the first message of the sender self test.\n\nSender',
    followup_body: 'Hi,\n\nThis is the follow-up. It should sit in the same thread as the first message.\n\nSender',
  };
  const first = d.gmail.sendFirst(row, cfg);
  row.thread_id = first.threadId; row.message_id = first.messageId;
  Utilities.sleep(20000);
  d.gmail.sendFollowup(row, cfg);
  SpreadsheetApp.getUi().alert('Sent 2 messages to ' + cfg.TEST_ADDRESS + '.\n\nCheck: one thread with both messages, and "Show original" shows SPF PASS, DKIM PASS, DMARC PASS.');
}
