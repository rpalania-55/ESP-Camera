# ESP32-CAM Shopping Board

This project photographs a shopping board at 8:00 AM and 6:00 PM Eastern time, extracts its text with Google Cloud Vision, and replaces a dedicated Google Doc with a clean bulleted list.

## Architecture

The ESP32-CAM sends a JPEG directly to a small Cloud Run HTTPS endpoint. Cloud Run authenticates the device, performs `DOCUMENT_TEXT_DETECTION`, cleans the detected lines, and updates the Google Doc through a dedicated service account. The image exists only in memory and is not written to Cloud Storage or logs.

## Prerequisites

- An ESP32-CAM with OV2640 camera and 4 MB PSRAM (the firmware defaults to the common AI Thinker pin map)
- A stable 5 V power supply and 2.4 GHz Wi-Fi
- Arduino IDE 2.x with the current Espressif ESP32 board package
- Python 3.12 for local backend tests
- Google Cloud SDK (`gcloud`), a billing-enabled project, and permission to create Cloud Run, IAM, and Secret Manager resources
- A new, dedicated, single-tab Google Doc

## 1. Verify the camera hardware

In Arduino IDE, install the `esp32` board package from Espressif and select **AI Thinker ESP32-CAM**. If the module cannot initialize with this project, compare its vendor pinout with the constants at the top of `firmware/shopping_board_camera.ino` before proceeding.

For programming, connect a USB-to-serial adapter using 5 V, GND, U0R/TX, and U0T/RX, and hold GPIO0 to GND while resetting to enter flash mode. Remove GPIO0 from GND and reset again after uploading. Use a supply that can handle camera/Wi-Fi current peaks; weak FTDI 3.3 V outputs commonly cause brownouts.

## 2. Prepare Google Cloud and deploy

Create a dedicated Google Doc named `Shopping List Automation`. Copy its ID from the URL:

```text
https://docs.google.com/document/d/DOCUMENT_ID/edit
```

Authenticate the CLI, then deploy from PowerShell:

```powershell
gcloud auth login
./deploy.ps1 -ProjectId "YOUR_PROJECT_ID" -DocumentId "YOUR_DOCUMENT_ID"
```

The script enables the required APIs, creates a least-privilege runtime service account, stores a random device token in Secret Manager, and deploys the public Cloud Run endpoint. Public invocation is necessary for the microcontroller, but the application rejects requests without the token.

When the script finishes:

1. Share the dedicated Google Doc as **Editor** with the printed runtime service-account email.
2. Save the printed Cloud Run capture URL and device token for the firmware configuration.
3. Do not put a service-account JSON key on the ESP32 or in this repository. Cloud Run uses its keyless runtime identity.

Re-running `deploy.ps1` without `-DeviceToken` rotates the device token. If rotating deliberately, update and reflash `secrets.h`. To retain the current token during a redeploy, pass it with `-DeviceToken`.

## 3. Configure and flash the ESP32

Copy the example secrets file:

```powershell
Copy-Item firmware/secrets.example.h firmware/secrets.h
```

Edit `firmware/secrets.h` with the 2.4 GHz Wi-Fi credentials, printed capture URL, device ID, and device token. Open `firmware/shopping_board_camera.ino` in Arduino IDE and upload it using these settings:

- Board: AI Thinker ESP32-CAM
- Partition Scheme: Huge APP
- PSRAM: Enabled
- Upload speed: 115200 if higher speeds are unreliable

Open Serial Monitor at 115200 baud after restarting. A power-on causes an immediate commissioning capture. After a successful or permanent-result upload, the device sleeps until the next 8:00 AM or 6:00 PM America/New_York occurrence. Network and retryable server failures wake again after 15 minutes.

The firmware validates Cloud Run TLS using the included Google Trust Services GTS Root R1 certificate. Its fingerprint is recorded in `firmware/certificates.h`; do not replace TLS verification with `setInsecure()`.

## 4. Verify the backend locally

The unit tests mock Google services and do not require credentials:

```powershell
python -m venv .venv
./.venv/Scripts/python -m pip install -r cloud/requirements-dev.txt
$env:PYTHONPATH = "cloud"
./.venv/Scripts/python -m pytest cloud/tests
```

For a deployed health check:

```powershell
Invoke-RestMethod "YOUR_CLOUD_RUN_URL/healthz"
```

For an end-to-end test without waiting for the ESP32, send a known JPEG:

```powershell
$headers = @{
    "X-Device-ID" = "shopping-board-camera"
    "X-Device-Token" = "YOUR_DEVICE_TOKEN"
}
Invoke-RestMethod -Method Post -Uri "YOUR_CLOUD_RUN_URL/v1/captures" -Headers $headers -ContentType "image/jpeg" -InFile "board.jpg"
```

Expected output reports `updated` or `unchanged` and the item count. Confirm the Doc contains only a `Shopping List` heading followed by the detected lines as bullets.

## Failure behavior and privacy

- Empty or implausible OCR returns HTTP 422 and leaves the existing Doc untouched.
- A multi-tab target is rejected to prevent content from being deleted from the wrong tab.
- Concurrent edits are protected with the document revision ID; one revision-conflict retry is allowed, followed by a full readback verification.
- Authentication, file-type, and size failures are not repeatedly retried by the device.
- JPEG data and OCR text are never intentionally logged or stored. Cloud logs contain only request metadata and counts.
- This automation owns the dedicated Doc body. Manual content in that Doc will be replaced at the next successful capture.

