  # Klipper Creality DWIN Touchscreen Bridge

  A lightweight, high-performance Python daemon that connects the stock **Creality DWIN (DGUS II)
Touchscreen** (found on the *Ender-5 S1*, *Ender-3 S1*, *Ender-3 V2*, etc.) directly to **Klipper**
and **Moonraker** via the Raspberry Pi GPIO UART port.

  No screen firmware reflashing is required — it works with Creality's stock DWIN firmware.

  ---

  ## ⚡ Features & Capabilities

  ### ✅ What works:
  - **Print File Explorer & Direct Launching:** Fetches G-code files from Moonraker, features 4-
item pagination with live page counters (`1 / N`), and 1-tap print starting directly from the
screen.
  - **Real-Time Print Monitoring:** Live extruder & bed temperatures (current/target), XYZ axis
positions, fan speed toggle, visual progress bar, print percentage, elapsed time, and calculated
remaining time.
  - **Print Job Controls:** Pause, Resume, and Cancel print jobs.
  - **Manual Movement & Homing:** Home all axes (`G28`), Home XY, and manual jog controls for X, Y,
and Z.
  - **Direct Filament Load & Unload:** Immediate extrusion and retraction with automatic nozzle
preheating to 200°C.
  - **Temperature Controls & Presets:** Manual temperature input, Cooldown, and customizable
dynamic PLA / ABS preheat presets.
  - **On-the-Fly Multilingual Support:** In-memory language switching (French, English, German,
Spanish, etc.) .
  - **Zero-Lag Asynchronous Dispatch:** Non-blocking G-code and Moonraker requests for instant
touch response (< 5ms).
  - **Delta-Cached UART Transmission:** Only transmits values when telemetry actually changes,
reducing UART bandwidth usage by ~95%.
  - **Zero-Log Smart USB Detection:** Checks hardware USB enumeration and restarts Klipper only
once upon printer power-on, preventing `klippy.log` bloat.

  ### ❌ What is NOT supported (Managed via Klipper / Mainsail):
  - **Machine Motion Parameters (Steps/mm, Max Velocity, Acceleration, Jerk):** Defined directly in
Klipper's `printer.cfg`.
  - **PID Tuning from Screen:** Performed directly via Klipper commands (`PID_CALIBRATE`).
  - **Bed Leveling Mesh Grid / Tramming View:** Handled via Mainsail / Fluidd web interface.
  - **Filament Sensor Toggle:** Configured in `printer.cfg` under `[filament_switch_sensor]`.

  ---

  ## 🔌 Hardware Wiring (Direct Connection)

  Connect the 4-pin screen ribbon cable directly to the Raspberry Pi 40-pin GPIO header:

  | Screen Cable Pin | Screen Function | Raspberry Pi GPIO Pin | Physical Pin # |
  | :--- | :--- | :--- | :--- |
  | **Pin 1** (Red) | **VCC (5V)** | 5V Power | **Pin 2** |
  | **Pin 2** (Yellow/White) | **Screen TX** | GPIO 15 (UART RXD) | **Pin 10** |
  | **Pin 3** (Green/Blue) | **Screen RX** | GPIO 14 (UART TXD) | **Pin 8** |
  | **Pin 4** (Black) | **GND** | Ground (GND) | **Pin 6** |

  > [!NOTE]
  > Ensure serial UART is enabled on your Raspberry Pi:
  > 1. Run `sudo raspi-config` -> **Interface Options** -> **Serial Port**.
  > 2. Select **No** to *"login shell over serial"*, and **Yes** to *"serial port hardware
enabled"*.
  > 3. Verify that `/dev/serial0` points to your primary UART.

  ---

  ## 🚀 Installation

  ### 1. Install Dependencies
  On your Raspberry Pi:
  ```bash
  sudo apt-get update
  sudo apt-get install -y python3-serial python3-pip

### 2. Download the Bridge Script

Clone this repository or copy klipper_dwin_bridge.py to /home/pi/:

  git clone https://github.com/FoxCraft67/Klipper-Creality-DWIN-Touchscreen-Bridge.git
  cd klipper-dwin-bridge
  cp klipper_dwin_bridge.py /home/pi/klipper_dwin_bridge.py

### 3. Test the Script Manually

  python3 /home/pi/klipper_dwin_bridge.py

──────
## ⚙️ Run Automatically as a Systemd Service

To ensure the bridge starts automatically on boot:

1. Create the systemd service unit file:

  sudo nano /etc/systemd/system/klipper-dwin.service

2. Paste the following configuration:

  [Unit]
  Description=Klipper Creality DWIN Touchscreen Bridge Daemon
  After=network.target moonraker.service
  Wants=moonraker.service

  [Service]
  Type=simple
  User=pi
  ExecStart=/usr/bin/python3 /home/pi/klipper_dwin_bridge.py
  Restart=always
  RestartSec=3

  [Install]
  WantedBy=multi-user.target

3. Enable and start the service:

  sudo systemctl daemon-reload
  sudo systemctl enable klipper-dwin.service
  sudo systemctl start klipper-dwin.service

4. Check the service status & live logs:

  sudo systemctl status klipper-dwin.service
  journalctl -u klipper-dwin.service -f
