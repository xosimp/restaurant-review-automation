"""No customer's data in the repository (audit #25 A01-01, R1).

The repository is public. A production export of the live customer's
invoice lines sat in tests/fixtures as a test fixture ("SELECT-only export",
supplier, invoice numbers, prices); it is now a synthetic history
(scripts/make_bar_invoice_fixture.py), and its invoice numbers were replaced
wherever tests and comments had copied them. This keeps it that way:

- nothing under tests/fixtures/ or docs/clients/ says it was exported from
  production (a "SELECT-only export", a "(rid N)" source);
- none of the real invoice numbers (or the distributor account number beside
  them) comes back anywhere in the tracked text — checked by hash, so this
  file does not republish them;
- no real customer's Google Place ID appears (hashed denylist; add one with
  `hashlib.sha256(place_id.encode()).hexdigest()[:16]`);
- the synthetic fixture says it is synthetic, and the old file stays gone.

The customer's own documents under docs/clients/ (recipes, chart of accounts,
checklists, DSR settings, the staff availability sheet) are a separate
decision (Will's): they are listed in the fix round's report, not judged here.
"""
import hashlib
import json
import os
import re
import subprocess

import pytest

pytestmark = pytest.mark.contract

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# SHA-256 (first 16 hex) of each invoice number in the removed production
# export, its zero-stripped form, and the account number beside them.
DENY_INVOICES = {
    "00dc75d3bd75aa2b", "00df567e61695e0d", "02b6c755649e51f4", "0452780d34418627", "045386cb5124d01e", "0480cb8851e9a452",
    "079268773431e4e7", "08adb973ba1b511d", "093d469c06f65983", "0bce32d154ef82ac", "0c59c1ee13f144f9", "0d06adf8a3a7aba1",
    "0dbaf25573277dff", "0ee4c04362b46e1c", "0f394e8de474b1d5", "11df0e17f6e876a8", "131658d89fbddf97", "164cd5bc525632d9",
    "170482f80e54c80f", "172034f0ec676179", "1817e98ee55220dc", "18c949dc2cea31a6", "1aefca86397a3313", "1b414bcb4dc11455",
    "1b4e8d8d5e31b6ff", "1dc62c0b4b92f16c", "1dccec3a2bb3597b", "1e1c8b25d9980c6e", "1e4ecdb1934a26c0", "252c7883eae4c8c9",
    "268910276013ed19", "2913c1ba47e3a4dc", "296221b3def1de06", "29ca18b8f5df7fae", "2a806d84547f822d", "2ab7c95a832eeb1b",
    "2f114daa410e26f1", "2f608922d08af8f2", "31a97719f353e2f8", "3201b0bf3f448ed8", "324405246670cefd", "34723d63c982978b",
    "3560df5231037aa6", "3768eda28b8ad3d8", "398dd27ea5499aab", "3b434dd3f6ca2248", "3b5f98ae125c86e1", "3c2fc9175eb8e070",
    "3cf992cc779c3690", "3e09bfdcd268a85d", "40e6df439ff10437", "42048b088ac1b1ed", "42c07f4a993ced7b", "45573c12f3360721",
    "45b34a663ca41223", "45b4bdffd6c1fe43", "4788b134421233c4", "487a3dbea7d377b1", "495d4b4d06d7775d", "49d2e3b7c83d82aa",
    "4b29ed23ae2ed679", "4b2badaeba315166", "4c7d605041819161", "4ccdbbb4ac6d32bf", "4e14987139110d88", "4e32f0f3cc48d0a9",
    "4eaee54da02aaeb7", "4f6c44a845139480", "500b0a54d4d672d7", "5061414738d87a52", "5175676c5eb815b7", "524c6b12b35a37ec",
    "5257a436875bd01f", "5548c8bcf4a09ee9", "55537dcdb31de776", "57095973ead3c70f", "571530496dbfa309", "588db6999c3dfc76",
    "5c177352c6069898", "5c3930cfa6875f75", "5c9f0708eb9658c2", "5dcc39602957c1aa", "5ebb7080e35eeb76", "603b9db4662d304d",
    "63ea55eb77dd3505", "63eb8c9bf8240c98", "664bc0f934472890", "667b96231d4b8d1c", "6750364ed0cc8f8c", "68370854760f6868",
    "689804860d4671af", "68b42432df6197a8", "690e9d609a3ebdf7", "69ee8e46b7650ac7", "6a885e2891feb4bb", "6ab9a59008c9fbf9",
    "6ba109389d9cecda", "6c94b695fac34549", "6ca4d1a165426d5a", "6d2e7b9989758616", "6dd2dae1d407778c", "75a428320a996f8b",
    "7665b9a435f35220", "76bd7ef0e522b80b", "777525e666b139d5", "777dcbec3e1b5391", "78665a31cbb8e3df", "7a162529abed1568",
    "7acb0e3431c3635f", "7c033ea97b2facbe", "7daa858d5bae6b7f", "7ea4e9deb334ed8d", "803f633664737587", "80867a622fab0679",
    "82387e0b3af2823f", "83675bc186bcb24d", "840a8a69b4af9e8a", "8419c7498b8957a5", "854f7d16e74ec679", "8687e8321f2a1292",
    "881fda8122f499a3", "88b1145d4e59d355", "88fa52428dcc5e7f", "89eda09d956d15ac", "8a6d6a4cf19cfc08", "8dd99367e450ced1",
    "8e288b8e644f49d5", "8e5c0ac0a16cb515", "8e5dfba05eaf216d", "8fc79e33db6d9328", "9271b1340d54c8d5", "93e903f87694dc7a",
    "94aaa720746ff4c3", "97967ff726dd572d", "97f556d130b13c4f", "9a547e59c844635f", "9da221b0f960600b", "9faaa8d3cb7d2c73",
    "a00b83fdbab72517", "a1fa6ec50508e342", "a245f68da579f4fe", "a26ba2a8eb342f34", "a2be3cadc99e9eac", "a328ca8a43b0c32c",
    "a42d92190aa6afed", "a61a60b98e86320a", "a67688d005b50ac9", "a7592edfa2c0ac9a", "ae5364d74ca8515a", "af1b3255836877d3",
    "afda204e917e58a6", "b213a0f6bd504094", "b25a1a58a7abf4cf", "b2bc0e0434fa1bb4", "b3feb6c3c493a479", "b4bfa22f46432c3a",
    "b53e005d5f520782", "b5f33e9089ba1516", "b69f9b7c67e1b1d8", "ba026bf8432f6016", "bb00b1d681f53b86", "bb6f9fc934001b0a",
    "bbcc20125d5bc66f", "bbf0cb0ab54fee9c", "bd63b3ed30ab8177", "bda2315c049282ff", "bec44ff2c1ea0e59", "bfb43335413aa81f",
    "c042afb36875ff32", "c077bc2eda28a1ef", "c09f485f29d31ba4", "c183fe9aab143481", "c32a0f7908a6b4e3", "c39677b71f8e0f7f",
    "c40d65e53d738b83", "c48caf589349d305", "c51afaedc4aec801", "ca4aae4bde1f049f", "ccb400b0b212e35c", "ce0915dfff3a33fd",
    "d1c24bd4c8d55f16", "d2c06e166ab7e5a7", "d3bd974a988b5fbd", "d5f16e29450ef0d3", "d7c2934e8c712cfd", "d7d13d55b3e40a23",
    "d8b6185ac18846cb", "d9b7380bf6917498", "d9b7acf3721c962a", "d9d3a233ea653403", "da4892aed4e7db10", "dbeac0b36d3d69f8",
    "dcea1ca2e4cacf8e", "ddb4a34dbb9aa0a8", "de82336a41ba77bd", "df6ec32d2bd3a4e1", "e0eb333530c832f0", "e26bad6747a83b94",
    "e389aebccc3780c5", "e42c690dc1ea0f58", "e49211f728a76b5f", "e4e258d6f2de2f98", "e51d01c3634f13a7", "e5b93819c7c4bbf9",
    "e645015e9cf17b3f", "e863ab8b06c65e66", "e88e58ea6a69ea2a", "e8d681db22b532e9", "e8f09b88fd550229", "e9ed2efed1dac3e4",
    "ed1552b7b70a26ab", "ed99862f0b9f6c7b", "ef39ad63b9c8574c", "f53bb016abedc9ee", "f7367812e99efc28", "f7e9dafa813be32e",
    "f9e045dea051402b", "fb2c64c81fea3db3", "fc73edeb30b4b522", "fcee264efcf6e7d0", "fe07304479e3fdd5", "fe4e77527e69a408",
    "fe6dcc0ffbeb5ddb", "fff5b8e178272495", "fff8d056c74f64ce",
}
# SHA-256 (first 16 hex) of real customers' Google Place IDs.
DENY_PLACE_IDS = {"9e23690cef809236"}

_EXPORT_MARK = re.compile(r"(?i)\bSELECT[- ]only\b[^\n]{0,60}\bexport|\(rid \d+\)|\bproduction export\b")
_TOKEN = re.compile(r"(?<![0-9A-Za-z])(?:[A-Z]{1,3}-?)?\d{6,}(?![0-9A-Za-z])")
_PLACE = re.compile(r"ChIJ[0-9A-Za-z_-]{10,}")
_TEXT = (".py", ".md", ".json", ".txt", ".csv", ".html", ".swift", ".yml", ".yaml", ".js", ".css", ".sql")


def _h(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def _tracked():
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT,
                         capture_output=True, text=True, timeout=30)
    files = out.stdout.split() if out.returncode == 0 else []
    if not files:                                   # not a git checkout: walk it
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "node_modules"]
            files += [os.path.relpath(os.path.join(dirpath, f), ROOT) for f in filenames]
    return [f for f in files if f.endswith(_TEXT) and os.path.exists(os.path.join(ROOT, f))]


def _read(rel):
    try:
        with open(os.path.join(ROOT, rel), encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def test_no_fixture_or_client_document_says_it_came_from_production():
    bad = []
    for base in ("tests/fixtures", "docs/clients"):
        for dirpath, _dirs, files in os.walk(os.path.join(ROOT, base)):
            for name in files:
                rel = os.path.relpath(os.path.join(dirpath, name), ROOT)
                m = _EXPORT_MARK.search(_read(rel)) if rel.endswith(_TEXT) else None
                if m:
                    bad.append(f"{rel}: {m.group(0)}")
    assert not bad, "A production export in the repository (it is public): %s" % bad


def test_no_real_invoice_number_comes_back():
    me = os.path.relpath(os.path.abspath(__file__), ROOT)
    hits = []
    for rel in _tracked():
        if rel == me:
            continue
        for tok in set(_TOKEN.findall(_read(rel))):
            forms = {tok, tok.lstrip("0")} | ({re.sub(r"^[A-Z]{1,3}-?", "", tok)} if not tok[0].isdigit() else set())
            if any(_h(f) in DENY_INVOICES for f in forms if f):
                hits.append(rel)
                break
    assert not hits, "A real invoice or account number from the removed production export is back in: %s" % hits


def test_no_real_place_id_appears():
    hits = [rel for rel in _tracked() for pid in set(_PLACE.findall(_read(rel))) if _h(pid) in DENY_PLACE_IDS]
    assert not hits, "A real customer's Google Place ID is in: %s" % hits


def test_the_bar_fixture_is_synthetic_and_the_export_is_gone():
    assert not os.path.exists(os.path.join(ROOT, "tests", "fixtures", "bar_ejs_invoice_lines.json"))
    with open(os.path.join(ROOT, "tests", "fixtures", "bar_invoice_lines.json"), encoding="utf-8") as fh:
        doc = json.load(fh)
    assert doc["source"].startswith("SYNTHETIC") and "make_bar_invoice_fixture.py" in doc["source"]


def test_the_checks_catch_what_they_guard(tmp_path):
    # The hash of a removed invoice number is in the denylist, and the marks
    # match the removed file's own source line.
    assert len(DENY_INVOICES) > 200 and all(len(h) == 16 for h in DENY_INVOICES)
    assert _EXPORT_MARK.search('"source": "A Restaurant (rid 5) lines, SELECT-only export used by an audit"')
    assert not _EXPORT_MARK.search('"source": "SYNTHETIC — scripts/make_bar_invoice_fixture.py"')
    assert _TOKEN.findall("inv W-1234567 and 0123456789, not 12345") == ["W-1234567", "0123456789"]
