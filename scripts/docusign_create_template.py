"""Create (or refresh) the production DocuSign template from the built
contract PDF, with every field attached to the PDF's anchor tokens.

    PYTHONPATH=. railway run python3 scripts/docusign_create_template.py

Reads the DocuSign env (integration key, private key, base URL, account,
user) exactly as docusign_helper does, so it targets whatever environment
Railway is pointed at. Prints the new template id; set DOCUSIGN_TEMPLATE_ID
to it. The old template is left in place (envelopes already sent from it
keep working).
"""
import base64
import os
import sys

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import docusign_helper as d  # noqa: E402
from scripts.build_contract_pdf import OUT  # noqa: E402

NAME = os.environ.get("DOCUSIGN_TEMPLATE_NAME", "Cavnar AI Service Agreement")


def _text(label, anchor, width=140, font_size="size10"):
    return {"tabLabel": label, "anchorString": anchor, "anchorUnits": "pixels", "anchorXOffset": "0", "anchorYOffset": "-3",
            "anchorIgnoreIfNotPresent": "false", "width": str(width), "height": "16", "font": "helvetica", "fontSize": font_size,
            "bold": "true", "locked": "true", "required": "false", "shared": "true", "documentId": "1"}


def build_body():
    pdf = base64.b64encode(open(OUT, "rb").read()).decode()
    client_tabs = {
        "signHereTabs": [{"anchorString": "{{sig_client}}", "anchorUnits": "pixels", "anchorXOffset": "0", "anchorYOffset": "-14", "documentId": "1"}],
        "dateSignedTabs": [{"anchorString": "{{date_client}}", "anchorUnits": "pixels", "anchorXOffset": "0", "anchorYOffset": "-3", "fontSize": "size9", "documentId": "1"}],
        "textTabs": [_text("restaurant_name", "{{restaurant_name}}", 150), _text("owner_name", "{{owner_name}}", 150),
                     _text("owner_email", "{{owner_email}}", 150, "size9"), _text("modules", "{{modules}}", 340, "size9"),
                     _text("setup_fee", "{{setup_fee}}"), _text("monthly_fee", "{{monthly_fee}}"), _text("annual_fee", "{{annual_fee}}")],
    }
    admin_tabs = {
        "signHereTabs": [{"anchorString": "{{sig_admin}}", "anchorUnits": "pixels", "anchorXOffset": "0", "anchorYOffset": "-14", "documentId": "1"}],
        "dateSignedTabs": [{"anchorString": "{{date_admin}}", "anchorUnits": "pixels", "anchorXOffset": "0", "anchorYOffset": "-3", "fontSize": "size9", "documentId": "1"}],
    }
    return {
        "name": NAME, "shared": "false", "emailSubject": "Your Cavnar AI Service Agreement",
        "documents": [{"documentId": "1", "name": "Cavnar AI Service Agreement.pdf", "fileExtension": "pdf", "documentBase64": pdf}],
        "recipients": {"signers": [
            {"roleName": "Client", "recipientId": "1", "routingOrder": "1", "tabs": client_tabs},
            {"roleName": "Admin", "recipientId": "2", "routingOrder": "2", "name": "Will Cavnar", "email": "will@cavnar.ai", "tabs": admin_tabs},
        ]},
    }


def main():
    tok = d.get_access_token()
    api = f"{d.BASE_URL}/restapi/v2.1/accounts/{d.ACCOUNT_ID}"
    h = {"Authorization": "Bearer " + tok, "Content-Type": "application/json"}
    r = requests.post(api + "/templates", headers=h, json=build_body())
    if r.status_code not in (200, 201):
        sys.exit("template create failed: %s %s" % (r.status_code, r.text[:500]))
    tid = r.json()["templateId"]
    v = requests.get(f"{api}/templates/{tid}", headers=h, params={"include": "recipients,tabs"}).json()
    for s in v.get("recipients", {}).get("signers", []):
        tabs = s.get("tabs") or {}
        print("  role", s["roleName"], "order", s.get("routingOrder"),
              {k: [x.get("tabLabel") or x.get("anchorString") for x in vv] for k, vv in tabs.items() if k in ("textTabs", "signHereTabs", "dateSignedTabs")})
    print("environment:", d.BASE_URL)
    print("DOCUSIGN_TEMPLATE_ID=" + tid)
    return tid


if __name__ == "__main__":
    main()
