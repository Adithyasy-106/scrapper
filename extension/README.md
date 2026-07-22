# Wish Cookie Sender Extension

This Chrome/Edge extension collects Instagram cookies from the current browser and posts them to the backend API.

## Install
1. Open `chrome://extensions` or `edge://extensions`.
2. Enable `Developer mode`.
3. Click `Load unpacked`.
4. Select this `extension/` folder.

## Use
1. Make sure you are logged into Instagram in the browser.
2. Open the extension popup.
3. Click `Send cookies`.
4. The backend at `http://localhost:5000/api/extension-extract` will receive the cookie data.

## Notes
- This extension only works if the backend is accessible from the browser.
- `manifest.json` includes localhost host permissions for development.
- To use with a deployed backend, update `popup.js` to point `BACKEND_URL` to your deployed server.
