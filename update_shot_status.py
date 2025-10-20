from shotgun_api3 import Shotgun
from flask import Flask, request, jsonify
import os
import requests
import json
from urllib.parse import quote
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
    "omt": "OMIT"
}

# --- FMP AUTH ---
def fmp_login():
    """Authenticate and return FMP session token"""
    url = f"{FMP_SERVER}/fmi/data/v2/databases/{FMP_DB}/sessions"
    auth_string = f"{FMP_USERNAME}:{FMP_PASSWORD}"
    auth_base64 = b64encode(auth_string.encode("utf-8")).decode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {auth_base64}",
    }
    r = requests.post(url, headers=headers)
    if r.status_code == 200:
        return r.json()["response"]["token"]
    else:
        raise Exception(f"FMP Login failed: {r.text}")

# --- FMP SCRIPT CALL ---
def fmp_update_status(token, sg_id, fmp_status):
    """Call a FileMaker script to update the Shot record"""
    script_name = "SG_update_status"  # <-- Name of your FM script
    url = f"{FMP_SERVER}/fmi/data/v2/databases/{FMP_DB}/scripts/{quote(script_name)}"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    param = json.dumps({"SG_ID": sg_id, "Status": fmp_status})
    payload = {"script.param": param}
    response = requests.post(url, headers=headers, json=payload)
    if response.status_code == 200:
        print(f"✅ Script called for SG_ID {sg_id}: {response.json()}")
        return True
    else:
        print(f"❌ Failed to call script for SG_ID {sg_id}: {response.text}")
        return False


# --- FLASK ROUTE ---
@app.route("/update_shot_status", methods=["GET", "POST"])
def update_shot_status():
    data = {}
    data.update(request.args)
    data.update(request.form)
    if request.is_json:
        data.update(request.get_json())

    selected_ids = data.get("selected_ids", "")
    ids = [int(x) for x in selected_ids.split(",") if x.strip().isdigit()]

    if not ids:
        return "No valid Version IDs received from SG.", 400

    # --- Get Versions + linked Shots ---
    versions = sg.find("Version", [["id", "in", ids]], ["id", "code", "entity.Shot.code", "entity.Shot.id", "entity.Shot.sg_status_list"])
    
    fmp_token = fmp_login()
    updated = 0
    skipped = 0
    
    for v in versions:
        shot = v.get("entity")
        if not shot or not isinstance(shot, dict):
            print(f"Skipping version {v['id']} (no linked Shot)")
            skipped += 1
            log.append({"version_id": v["id"], "note": "No linked Shot"})
            continue
        
        shot_data = sg.find_one("Shot", [["id", "is", shot["id"]]], ["id", "sg_status_list"])
        if not shot_data:
            skipped += 1
            log.append({"version_id": v["id"], "note": f"Shot {shot['id']} not found"})
            continue

        sg_id = shot_data["id"]
        sg_status = shot_data.get("sg_status_list")
        fmp_status = STATUS_MAP.get(sg_status, "Unknown")

        if fmp_status == "Unknown":
            print(f"Skipping SG_ID {sg_id} with unmapped status {sg_status}")
            skipped += 1
            continue

        success = fmp_update_status(fmp_token, sg_id, fmp_status)
        if success:
            updated += 1
        else:
            skipped += 1
            log.append({"shot_id": sg_id, "note": "FMP script failed"})

    # --- Return JSON ---
    result = {
        "message": f"✅ Updated {updated} shots in FileMaker. Skipped {skipped}.",
        "updated": updated,
        "skipped": skipped,
 
    }

    if debug:
        result["debug_log"] = log

    return jsonify(result)

# --- RUN APP ---
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001)
