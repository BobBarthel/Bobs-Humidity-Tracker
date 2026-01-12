# Bobs Humidity Tracker

Software for using Sensirion demoboards (SHT31 and SHT4x) on Mac and PC and logging their data.

<img width="1012" height="732" alt="Screenshot 2026-01-12 at 16 42 27" src="https://github.com/user-attachments/assets/f80d20f2-55cf-4bf6-8d74-95037c2b42b4" />


## BLE service reference

The BLE Service definition used by this project is based on:
https://sensirion.github.io/ble-services/#/services

## Usage

1. Power on your Sensirion SHT31 or SHT4x demoboard.
2. Enable BLE by holding the button.
3. Launch the app on your Mac or PC.

After the first time, you can connect faster from "Dashboard" by clicking "Connect".

4. Go to the "Devices" tab.
5. Click "Scan for Devices".
6. Select your device (for example, "SHT40 Gadget" or "Smart Humigadget").
7. Click "Use" and wait for the compatibility check to finish.
8. Return to the "Dashboard".
9. View live readings and enable "Autosave CSV" to start logging.

## Build

### macOS

Run:
`./build_mac.sh`

### Windows

Run:
`build_windows.bat`

## Releases

Prebuilt releases are provided for macOS and Windows. Download the latest release from the project releases page.

## License

MIT. See `LICENSE`.
