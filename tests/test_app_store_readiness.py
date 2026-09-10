"""Audit #8: the App Store submission facts that must not silently drift.

Two of these caused the DO NOT SHIP verdict: the privacy manifest declared
that the app collects nothing while it collects seven categories, and there
was no account a reviewer could sign in with while self-serve signup is off.
"""
import os
import plistlib

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(ROOT, "ios", "CavnarAI", "CavnarAI", "PrivacyInfo.xcprivacy")


@pytest.fixture(scope="module")
def manifest():
    with open(MANIFEST, "rb") as fh:
        return plistlib.load(fh)


def test_the_manifest_declares_what_the_app_actually_collects(manifest):
    """An empty NSPrivacyCollectedDataTypes beside a populated nutrition
    label is a contradiction Apple checks for."""
    declared = {d["NSPrivacyCollectedDataType"] for d in manifest["NSPrivacyCollectedDataTypes"]}
    for required in ("NSPrivacyCollectedDataTypeEmailAddress",
                     "NSPrivacyCollectedDataTypeName",
                     "NSPrivacyCollectedDataTypePhoneNumber",
                     "NSPrivacyCollectedDataTypeOtherFinancialInfo",
                     "NSPrivacyCollectedDataTypeDeviceID"):
        assert required in declared, f"{required} is collected but not declared"


def test_nothing_is_declared_as_tracking(manifest):
    assert manifest["NSPrivacyTracking"] is False
    assert manifest["NSPrivacyTrackingDomains"] == []
    for d in manifest["NSPrivacyCollectedDataTypes"]:
        assert d["NSPrivacyCollectedDataTypeTracking"] is False


def test_every_collected_type_is_complete(manifest):
    """A missing purpose or linkage flag fails validation at upload."""
    for d in manifest["NSPrivacyCollectedDataTypes"]:
        for key in ("NSPrivacyCollectedDataTypeLinked",
                    "NSPrivacyCollectedDataTypeTracking",
                    "NSPrivacyCollectedDataTypePurposes"):
            assert key in d, f"{d['NSPrivacyCollectedDataType']} is missing {key}"
        assert d["NSPrivacyCollectedDataTypePurposes"], "purposes cannot be empty"


def test_required_reason_apis_are_declared(manifest):
    reasons = {d["NSPrivacyAccessedAPIType"]: d["NSPrivacyAccessedAPITypeReasons"]
               for d in manifest["NSPrivacyAccessedAPITypes"]}
    assert reasons.get("NSPrivacyAccessedAPICategoryUserDefaults") == ["CA92.1"]
    assert reasons.get("NSPrivacyAccessedAPICategoryFileTimestamp") == ["C617.1"]


def test_the_review_account_seeder_exists_and_is_safe():
    """Guideline 2.1: with signup closed, a reviewer cannot get in without it."""
    path = os.path.join(ROOT, "scripts", "seed_review_account.py")
    assert os.path.exists(path)
    src = open(path, encoding="utf-8").read()
    assert '"two_fa_enabled": 0' in src, "a reviewer cannot receive a 2FA code"
    assert '"billing_status": "internal"' in src, "the account must be entitled but not a client"
    assert '"reviews_live": 0' in src, "the scheduled fetch would overwrite the seeded data"


def test_the_submission_doc_covers_the_payment_question():
    """Without this note a reviewer sees Stripe and reaches for 3.1.1."""
    import re
    raw = open(os.path.join(ROOT, "docs", "app-store-submission.md"), encoding="utf-8").read()
    # The note is a markdown blockquote, so sentences wrap across lines with a
    # "> " prefix. Flatten before matching or the assertions become a test of
    # where the line breaks happen to fall.
    doc = re.sub(r"\s+", " ", raw.replace("\n> ", " ").replace("\n>", " "))
    assert "PAYMENTS:" in doc
    assert "does not offer any purchase" in doc
    assert "no subscribe, upgrade or checkout flow" in doc.lower()
    assert "ACCOUNT DELETION:" in doc


def test_the_nutrition_label_table_matches_the_manifest(manifest):
    """The doc is what gets typed into App Store Connect. If it and the
    manifest disagree, that disagreement ships."""
    doc = open(os.path.join(ROOT, "docs", "app-store-submission.md"), encoding="utf-8").read()
    human = {
        "NSPrivacyCollectedDataTypeEmailAddress": "Email Address",
        "NSPrivacyCollectedDataTypeName": "Name",
        "NSPrivacyCollectedDataTypePhoneNumber": "Phone Number",
        "NSPrivacyCollectedDataTypeOtherFinancialInfo": "Other Financial Info",
        "NSPrivacyCollectedDataTypeDeviceID": "Device ID",
        "NSPrivacyCollectedDataTypeCustomerSupport": "Customer Support",
        "NSPrivacyCollectedDataTypeOtherDiagnosticData": "Other Diagnostic Data",
    }
    for d in manifest["NSPrivacyCollectedDataTypes"]:
        label = human.get(d["NSPrivacyCollectedDataType"])
        assert label, f"{d['NSPrivacyCollectedDataType']} has no row in the submission doc"
        assert label in doc, f"'{label}' is in the manifest but not in the doc's table"


def test_release_cannot_be_repointed_away_from_production():
    """Scoped to the Release branch of baseURL itself. The rest of the file
    legitimately mentions the override — the #if DEBUG helpers implement it,
    and a comment explains the bug where Release used to read it."""
    src = open(os.path.join(ROOT, "ios", "CavnarAI", "CavnarAI", "Core", "AppEnvironment.swift"),
               encoding="utf-8").read()
    body = src.split("static var baseURL", 1)[1]
    release_branch = body.split("#else", 1)[1].split("#endif", 1)[0]
    assert "https://dashboard.cavnar.ai" in release_branch
    # Only the comment may name it; no code in this branch may read it.
    code = [ln for ln in release_branch.splitlines() if not ln.strip().startswith("//")]
    assert not any("CAVNAR_API_BASE_URL" in ln for ln in code), \
        "Release reads an environment override again"
    assert not any("ProcessInfo" in ln for ln in code)
