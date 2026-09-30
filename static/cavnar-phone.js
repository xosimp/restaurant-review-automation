/* Phone numbers as an owner reads them: "(334) 568-9292" (Will, 9/29/26).
   fmtPhone(s) formats any US number for display, however it was stored
   ("+13345689292", "334.568.9292"); anything else comes back as it was -
   the same rule as auth.display_phone. Typing into a phone field
   (input[type=tel], or any input with data-phone) formats as you go and
   keeps the caret after the same digit. A one-time-code field
   (inputmode="numeric") and a number starting "+" and not "+1" are left
   alone. ES5 only. */
(function () {
  function digitsOf(s) { return String(s == null ? '' : s).replace(/\D/g, ''); }

  function fmtPhone(s) {
    var raw = String(s == null ? '' : s).replace(/^\s+|\s+$/g, ''), d = digitsOf(raw);
    if (d.length === 11 && d.charAt(0) === '1') d = d.slice(1);
    else if (raw.charAt(0) === '+' && raw.slice(0, 2) !== '+1') return raw;
    if (d.length !== 10) return raw;
    return '(' + d.slice(0, 3) + ') ' + d.slice(3, 6) + '-' + d.slice(6);
  }

  // The partial shape while typing: "(334", "(334) 56", "(334) 568-92".
  function fmtPartial(d) {
    if (!d) return '';
    if (d.length <= 3) return '(' + d;
    if (d.length <= 6) return '(' + d.slice(0, 3) + ') ' + d.slice(3);
    return '(' + d.slice(0, 3) + ') ' + d.slice(3, 6) + '-' + d.slice(6, 10);
  }

  function isPhoneField(el) {
    if (!el || el.tagName !== 'INPUT') return false;
    if (el.hasAttribute('data-phone')) return true;
    return el.type === 'tel' && el.getAttribute('inputmode') !== 'numeric';
  }

  function onType(e) {
    var el = e.target;
    if (!isPhoneField(el)) return;
    var v = el.value;
    if (v.charAt(0) === '+' && v.slice(0, 2) !== '+1') return;   // international: as typed
    var caret = typeof el.selectionStart === 'number' ? el.selectionStart : v.length;
    var before = digitsOf(v.slice(0, caret)).length, d = digitsOf(v);
    if (d.length === 11 && d.charAt(0) === '1') { d = d.slice(1); before = Math.max(0, before - 1); }
    if (d.length > 10) return;                                     // longer than a US number: leave it
    var out = fmtPartial(d);
    if (out === v) return;
    el.value = out;
    // Put the caret back after the same digit it followed.
    var pos = 0, seen = 0;
    while (pos < out.length && seen < before) { if (/\d/.test(out.charAt(pos))) seen++; pos++; }
    if (before === 0) pos = out.length && d.length ? 1 : 0;
    try { el.setSelectionRange(pos, pos); } catch (err) { /* a field that refuses a selection keeps the end */ }
  }

  // A field filled from saved data reads formatted too.
  function tidy(root) {
    var els = (root || document).querySelectorAll('input[type=tel], input[data-phone]');
    for (var i = 0; i < els.length; i++) if (isPhoneField(els[i]) && els[i].value) els[i].value = fmtPhone(els[i].value);
  }

  document.addEventListener('input', onType, true);
  document.addEventListener('focusout', function (e) { if (isPhoneField(e.target) && e.target.value) e.target.value = fmtPhone(e.target.value); }, true);
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', function () { tidy(); });
  else tidy();

  window.fmtPhone = fmtPhone;
  window.cavPhoneTidy = tidy;
})();
