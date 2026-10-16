// scripts/backoffice_read_counts.js: read Back Office's submitted inventories.
//
// Back Office (accounting4.restacct.com) has no API and no count export.
// Run this in a signed-in Back Office tab (a login with Inventory access,
// e.g. Erik's), as an async function body: it only GETs pages. It returns
// {"inventory_counts": [...]} for scripts/backoffice_import.py:
//
//   railway ssh "cd /app && /opt/venv/bin/python scripts/backoffice_import.py --rid 5 --file - --apply" < counts.json
//
// Each period: {start, end, status, gls: [{gl, name, amount}], items:
// [{location, item, gl_name, unit, qty, total}]}. Only "Submitted" periods
// are stored (backoffice_counts.save_counts); pass SINCE as an M/D/YYYY
// period end to skip older ones, and FORCE to also read unsubmitted
// periods (a parsing check only; the importer still skips them).
var SINCE = typeof SINCE === 'undefined' ? null : SINCE;
var FORCE = typeof FORCE === 'undefined' ? false : FORCE;

function txt(el) { return el ? (el.textContent || '').replace(/\s+/g, ' ').trim() : ''; }
function md(s) { var m = /(\d+)\/(\d+)\/(\d{4})/.exec(s || ''); return m ? [+m[3], +m[1], +m[2]] : null; }
function after(a, b) { a = md(a); b = md(b); if (!a || !b) return true; return (a[0] - b[0]) * 1e4 + (a[1] - b[1]) * 100 + (a[2] - b[2]) > 0; }
async function page(url) {
  var r = await fetch(url, { credentials: 'include' });
  if (/Login/i.test(r.url)) throw new Error('signed out of Back Office');
  return new DOMParser().parseFromString(await r.text(), 'text/html');
}

var list = await page('/Inventory');
var periods = [];
list.querySelectorAll('li').forEach(function (li) {
  var spans = Array.prototype.map.call(li.querySelectorAll('span'), txt).filter(function (s) { return s; });
  var dates = spans.filter(function (s) { return /^\d+\/\d+\/\d{4}$/.test(s); });
  if (dates.length < 2) return;
  var html = li.innerHTML, m = /unitFinancialCalendarId=([0-9a-f-]{36})/i.exec(html);
  // The status sits just before period start, period end and the expected
  // invoice date; the row's dialogs (Cancel Submittal…) come before it.
  var before = spans.slice(0, spans.indexOf(dates[0])).filter(function (s) { return s.length <= 20; });
  periods.push({ status: before[before.length - 1] || '', start: dates[0], end: dates[1], id: m ? m[1] : null });
});

var out = [];
for (var p of periods) {
  if (!p.id || (SINCE && !after(p.end, SINCE) && p.end !== SINCE)) continue;
  if (!/submitted/i.test(p.status) && !FORCE) continue;
  var d = await page('/Inventory/GetInventorySubmittal?unitFinancialCalendarId=' + p.id);
  var glById = {}, gls = [];
  d.querySelectorAll('input[name$=".Amount.Value"][name^="InventoryGLAccounts"]').forEach(function (inp) {
    var row = inp.closest('li, tr, .row');
    var cells = Array.prototype.map.call(row.querySelectorAll('div, td'), txt).filter(function (s) { return s; });
    var gl = cells.filter(function (s) { return /^\d{4}$/.test(s); })[0] || null;
    var name = cells.filter(function (s) { return /[A-Z]{3}/.test(s) && !/\$/.test(s); })[0] || null;
    var idx = /\[(\d+)\]/.exec(inp.name)[1];
    var ref = d.querySelector('input[name="InventoryGLAccounts[' + idx + '].InventoryGLAccountReferenceId"]');
    if (ref) glById[ref.value] = name;
    gls.push({ gl: gl, name: name, amount: inp.value });
  });
  var items = [], seen = {};
  var re = /(?:open|view)CountSheet\('([0-9a-f-]{36})'\)/g, html = d.body.innerHTML, mm;
  while ((mm = re.exec(html))) {
    if (seen[mm[1]]) continue;
    seen[mm[1]] = 1;
    var btn = d.querySelector('[onclick*="' + mm[1] + '"]');
    var row = btn && btn.closest('.row');
    var loc = row ? txt(row.querySelectorAll('.col')[1]) : '';
    var cs = await page('/Inventory/CountSheet/?inventorySubmittalLocationId=' + mm[1] + '&readOnly=true');
    cs.querySelectorAll('input[name$="].Quantity"]').forEach(function (q) {
      var idx = /\[(\d+)\]/.exec(q.name)[1];
      var r = q.closest('.row'), cols = r ? r.querySelectorAll('.col') : [];
      var glIn = cs.querySelector('input[name="Items[' + idx + '].CountSheetItem.InventoryGLAccountId"]');
      var tot = cs.querySelector('input[name="Items[' + idx + '].Total.Value"]');
      items.push({
        location: loc,
        item: txt(r && r.querySelector('.item-name-column')),
        gl_name: glIn ? (glById[glIn.value] || glIn.value) : null,
        unit: cols.length > 5 ? txt(cols[5]) : null,
        qty: q.value,
        total: tot ? tot.value : txt(r && r.querySelector('.rsi-total-column'))
      });
    });
  }
  out.push({ start: p.start, end: p.end, status: p.status, gls: gls, items: items });
}
return { inventory_counts: out, periods_listed: periods.length };
