import XCTest
@testable import CavnarAI

final class AccountSummaryTests: XCTestCase {
    func testDecodesFullAccountSummary() throws {
        let json = """
        {
          "ok": true,
          "profile": {
            "restaurant_name": "Gia Mia", "location_name": null,
            "owner_name": "Will", "owner_email": "will@x.com", "owner_phone": "312-555-0100",
            "neighborhood": "River North", "vibe": "Upscale casual", "known_for": "Pasta",
            "voice_notes": "Warm and direct", "never_say": null, "menu_notes": null,
            "timezone": "America/Chicago", "sign_off_name": "Will", "response_language": null,
            "tone_preset": "warm", "open_times_json": null, "close_times_json": null, "skip_holidays": null
          },
          "account": {
            "username": "will", "email": "will@x.com",
            "two_fa_enabled": true, "login_notify": false, "marketing_emails_opt_out": false,
            "recovery_email": "backup@x.com", "recovery_email_pending": null
          },
          "connections": {
            "google_business": {"connected": true, "last_synced": null},
            "instagram": {"connected": false, "last_synced": null},
            "toast": {"connected": true, "last_synced": "2026-01-01T00:00:00"},
            "square": {"connected": false, "last_synced": null},
            "clover": {"connected": false, "last_synced": null}
          },
          "alerts": {
            "contacts": [{"id": 1, "name": "Will", "phone": "+13125550100", "sms_consent": true}],
            "settings": {
              "alert_1star": true, "alert_2star": false, "alert_health": false,
              "alert_neg_spike": false, "alert_negative_trend": false, "alert_no_response": false,
              "alert_5star": false, "alert_labor_over": true, "urgent_via_sms": true,
              "urgent_via_email": true, "digest_enabled": true, "digest_day": "monday",
              "alert_quiet_start": null, "alert_quiet_end": null,
              "al_1star_push": true, "al_2star_push": true, "al_5star_push": false,
              "al_health_push": true, "al_spike_push": true, "al_unres_push": true,
              "alert_health_bypass_quiet": true, "alert_food_waste": false,
              "alert_ai_visibility_drop": true, "alert_extra_emails": "chef@x.com", "push_sound": true
            }
          },
          "reviews": {"auto_approve_5star": true, "auto_approve_daily_cap": 3, "auto_approve_paused": false, "auto_approved_today": 2},
          "data": {"data_retention_months": 12}
        }
        """
        let summary = try JSONDecoder.cavnar.decode(AccountSummary.self, from: Data(json.utf8))
        XCTAssertEqual(summary.profile.restaurantName, "Gia Mia")
        XCTAssertEqual(summary.profile.neighborhood, "River North")
        XCTAssertTrue(summary.account.twoFAEnabled)
        XCTAssertTrue(summary.connections.googleBusiness.connected)
        XCTAssertFalse(summary.connections.instagram.connected)
        XCTAssertEqual(summary.connections.toast.lastSynced, "2026-01-01T00:00:00")
        XCTAssertEqual(summary.alerts.contacts.count, 1)
        XCTAssertEqual(summary.alerts.contacts[0].name, "Will")
        XCTAssertTrue(summary.alerts.settings.alert1star)
        XCTAssertEqual(summary.alerts.settings.digestDay, "monday")
        XCTAssertEqual(summary.profile.signOffName, "Will")
        XCTAssertEqual(summary.profile.tonePreset, "warm")
        XCTAssertEqual(summary.account.recoveryEmail, "backup@x.com")
        XCTAssertTrue(summary.alerts.settings.alertHealthBypassQuiet)
        XCTAssertEqual(summary.alerts.settings.alertExtraEmails, "chef@x.com")
        XCTAssertTrue(summary.reviews.enabled)
        XCTAssertEqual(summary.reviews.dailyCap, 3)
        XCTAssertEqual(summary.reviews.approvedToday, 2)
        XCTAssertEqual(summary.data.retentionMonths, 12)
    }

    func testDecodesTrustedDeviceAndActivityEvent() throws {
        let device = try JSONDecoder.cavnar.decode(TrustedDevice.self, from: Data("""
        {"id": 4, "label": "iPhone · app", "created_at": "2026-09-01 10:00:00", "last_used_at": null, "expires_at": "2026-10-01 10:00:00"}
        """.utf8))
        XCTAssertEqual(device.label, "iPhone · app")
        XCTAssertNil(device.lastUsedAt)
        let event = try JSONDecoder.cavnar.decode(AccountActivityEvent.self, from: Data("""
        {"type": "password_changed", "label": "Password changed", "detail": null, "actor": "will", "created_at": "2026-09-03T17:00:00"}
        """.utf8))
        XCTAssertEqual(event.label, "Password changed")
        XCTAssertEqual(event.actor, "will")
    }

    func testDecodesAccountSessionWithDeviceType() throws {
        let json = """
        {"token_hint": "abc123", "is_current": true, "created_at": "2026-01-01 00:00:00",
         "last_active": "2026-01-02 00:00:00", "ip_address": "1.2.3.4",
         "device_type": "ios", "label": "iPhone (Cavnar AI app)"}
        """
        let session = try JSONDecoder.cavnar.decode(AccountSession.self, from: Data(json.utf8))
        XCTAssertTrue(session.isCurrent)
        XCTAssertEqual(session.deviceType, "ios")
        XCTAssertEqual(session.label, "iPhone (Cavnar AI app)")
    }

    func testDecodesBillingSummaryNoCustomer() throws {
        let json = """
        {"ok": false, "reason": "no_customer"}
        """
        let billing = try JSONDecoder.cavnar.decode(BillingSummary.self, from: Data(json.utf8))
        XCTAssertFalse(billing.ok)
        XCTAssertEqual(billing.reason, "no_customer")
        XCTAssertNil(billing.amount)
    }

    func testDecodesBillingSummaryActiveSubscription() throws {
        let json = """
        {"ok": true, "status": "active", "next_date": "8/1/2026", "amount": "$300/mo",
         "payment_method": "Visa ending 4242", "portal_url": "https://billing.stripe.com/xyz"}
        """
        let billing = try JSONDecoder.cavnar.decode(BillingSummary.self, from: Data(json.utf8))
        XCTAssertTrue(billing.ok)
        XCTAssertEqual(billing.amount, "$300/mo")
        XCTAssertEqual(billing.portalURL, "https://billing.stripe.com/xyz")
    }
}
