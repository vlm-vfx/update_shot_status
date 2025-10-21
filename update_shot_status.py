from shotgun_api3 import Shotgun
from flask import Flask, request
import os
import requests
import json
from base64 import b64encode

app = Flask(__name__)

# --- CONFIG ---
SG_URL = os.environ.get("SG_URL")
SG_SCRIPT_NAME = os.environ.get("SG_SCRIPT_NAME")
SG_API_KEY = os.environ.get("SG_API_KEY")

FMP_SERVER = os.environ.get("FMP_SERVER")
FMP_DB = os.environ.get("FMP_DB")
FMP_USERNAME = os.environ.get("FMP_USERNAME")
FMP_PASSWORD = os.environ.get("FMP_PASSWORD")

# --- CONNECT TO SHOTGRID ---
sg = Shotgun(SG_URL, script_name=SG_SCRIPT_NAME, api_key=SG_API_KEY)

# --- STATUS MAP (SG → FMP) ---
STATUS_MAP = {
    "wtg": "NEW",
    "ip": "IN PROGRESS",
    "hld": "ON HOLD",
    "profi": "NEED POST APPROVAL",
    "apr": "APPROVED",
    "omt": "OMIT",
}


# -------------------------------------------------------
# FILEMAKER HELPERS
# -------------------------------------------------------
def fmp_login():
    """Authenticate and return FMP session token"""
    url = f"{FMP_SERVER}/fmi/data/vLatest/databases/{FMP_DB}/sessions"
    auth_string = f"{FMP_USERNAME}:{FMP_PASSWORD}"
    auth_base64 = b64encode(auth_string.encode("utf-8")).decode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {auth_base64}",
    }
    r = requests.post(url, headers=headers)
    if r.status_code == 200:
        return r.json()["response"]["token"]
    raise Exception(f"❌ FMP Login failed: {r.text}")


def fmp_update_status(token, sg_id, fmp_status, debug=False):
    """Find and update a record in FileMaker where SG_ID matches"""
    try:
        # --- Find matching record ---
        url_find = f"{FMP_SERVER}/fmi/data/vLatest/databases/{FMP_DB}/layouts/status_update/_find"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        }
        query = {"query": [{"SG_ID": str(sg_id)}]}

        if debug:
            print(f"🔍 Querying FMP for SG_ID={sg_id}")

        find_response = requests.post(url_find, headers=headers, json=query)
        if find_response.status_code != 200:
            print(f"⚠ FMP find failed for SG_ID={sg_id}: {find_response.text}")
            return False

        data = find_response.json().get("response", {}).get("data", [])
        if not data:
            print(f"⚠ No matching FMP record found for SG_ID={sg_id}")
            return False

        record_id = data[0]["recordId"]

        # --- Update the Status field ---
        url_update = f"{FMP_SERVER}/fmi/data/vLatest/databases/{FMP_DB}/layouts/status_update/records/{record_id}"
        update_data = {"fieldData": {"Status": fmp_status}}
        update_response = requests.patch(url_update, headers=headers, json=update_data)

        if update_response.status_code == 200:
            if debug:
                print(f"✅ Updated FMP record {record_id} (SG_ID={sg_id}) → {fmp_status}")
            return True
        else:
            print(f"❌ Failed to update SG_ID={sg_id}: {update_response.text}")
            return False

    except Exception as e:
        print(f"❌ Exception updating SG_ID={sg_id}: {e}")
        return False


# -------------------------------------------------------
# FLASK ROUTE
# -------------------------------------------------------
@app.route("/update_shot_status", methods=["GET", "POST"])
def update_fmp_status():
    """Triggered by ShotGrid AMI – syncs shot status to FileMaker"""
    data = {}
    data.update(request.args)
    data.update(request.form)
    if request.is_json:
        data.update(request.get_json())

    debug = str(data.get("debug", "false")).lower() in ("1", "true", "yes")
    selected_ids = data.get("selected_ids", "")
    ids = [int(x) for x in selected_ids.split(",") if x.strip().isdigit()]

    if not ids:
        return "<h3>⚠ No valid Version IDs received from ShotGrid.</h3>", 400

    print("\n📡 Received AMI Request")
    print(f"   → Version IDs: {ids}")
    if debug:
        print(f"   → Debug mode: {debug}")

    try:
        versions = sg.find(
            "Version",
            [["id", "in", ids]],
            ["id", "code", "entity", "entity.Shot.id", "entity.Shot.sg_status_list"],
        )
    except Exception as e:
        return f"<h3>❌ ShotGrid query failed: {e}</h3>", 500

    try:
        fmp_token = fmp_login()
    except Exception as e:
        return f"<h3>❌ FileMaker login failed: {e}</h3>", 500

    updated, skipped = 0, 0

    for v in versions:
        entity = v.get("entity")
        if not entity or entity.get("type") != "Shot":
            if debug:
                print(f"⚠ Skipping Version {v['id']} (no linked Shot)")
            skipped += 1
            continue

        shot_id = entity["id"]
        shot = sg.find_one("Shot", [["id", "is", shot_id]], ["sg_status_list"])
        if not shot:
            skipped += 1
            continue

        sg_id = shot_id
        sg_status = shot.get("sg_status_list")
        fmp_status = STATUS_MAP.get(sg_status, "Unknown")

        if fmp_status == "Unknown":
            if debug:
                print(f"⚠ Skipping SG_ID={sg_id} (unmapped SG status: {sg_status})")
            skipped += 1
            continue

        if debug:
            print(f"\n→ SG_ID={sg_id} | SG Status={sg_status} | FMP Status={fmp_status}")

        success = fmp_update_status(fmp_token, sg_id, fmp_status, debug)
        if success:
            updated += 1
        else:
            skipped += 1

    print(f"\n✨ Finished updating — {updated} updated, {skipped} skipped.")

    # --- Return clean HTML summary ---
    color = "#4CAF50" if updated > 0 else "#EAB308"
    emoji = "✅" if updated > 0 else "⚠️"
    html = f"""
    <html>
    <body style='font-family: Arial; text-align: center; padding: 20px;'>
        <h2>{emoji} FileMaker Sync Complete</h2>
        <p style='font-size: 18px;'>Updated: <b>{updated}</b> &nbsp;&nbsp; Skipped: <b>{skipped}</b></p>
        <p style='color: gray; font-size: 14px;'>Debug: {debug}</p>
    </body>
    </html>
    """
    return html, 200


# -------------------------------------------------------
# MAIN
# -------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=False)
