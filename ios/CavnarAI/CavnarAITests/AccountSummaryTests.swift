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
            "voice_notes": "Warm and direct", "never_say": null, "menu_notes": null
          },
          "account": {
            "username": "will", "email": "will@x.com",
            "two_fa_enabled": true, "login_notify": false
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
              "urgent_via_email": true, "digest_enabled": true, "digest_day": "monday"
            }
          }
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
