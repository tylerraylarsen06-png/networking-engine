/**
 * Bridge.gs: web app entry points. Deploy as: Execute as Me, Who has access: Anyone.
 * Every request must carry the token stored in Script Properties (BRIDGE_TOKEN). Without a valid
 * token nothing is read or written. The Python side is bridge.py.
 */

function jsonOut(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

function doGet() {
  return jsonOut({ ok: true, service: 'outreach-sender' });    // health check only, no data
}

function doPost(e) {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(30000)) return jsonOut({ ok: false, error: 'busy, retry' });
  try {
    const req = JSON.parse(e.postData.contents);
    const token = PropertiesService.getScriptProperties().getProperty('BRIDGE_TOKEN');
    return jsonOut(handleBridge(makeDeps(), req, token));
  } catch (err) {
    return jsonOut({ ok: false, error: 'bad request' });
  } finally {
    lock.releaseLock();
  }
}
