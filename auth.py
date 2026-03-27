import time
import json
import base64
import hmac
import hashlib

#request_url is what???
def get_headers(api_key, api_secret, request_url="/v2/order-events"):
    #nonce generation, seconds
    nonce = int(time.time())
    # creating the payload

    '''payload_data = {
        "request": request_url,
        "nonce": nonce,
    }

    payload_json = json.dumps(payload_data)
    b64_payload = base64.b64encode(payload_json.encode()).decode()'''
    #replace above lines 13-19 with line 21
    b64_payload = base64.b64encode(str(nonce).encode()).decode()

    signature = hmac.new(
        api_secret.strip().encode(),
        b64_payload.encode(),
        hashlib.sha384
    ).hexdigest()

    return {
        "X-GEMINI-APIKEY": api_key.strip(),
        "X-GEMINI-PAYLOAD": b64_payload,
        "X-GEMINI-SIGNATURE": signature,
        "X-GEMINI-NONCE": str(nonce),
        "Cache-Control": "no-cache"
    }