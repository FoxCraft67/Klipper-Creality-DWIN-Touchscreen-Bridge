#!/usr/bin/env python3
"""
Klipper DWIN Touchscreen Bridge Daemon
Bridges Creality DWIN DGUS II touchscreen LCD with Klipper/Moonraker via Raspberry Pi UART.
"""

import os
import glob
import serial
import time
import urllib.request
import urllib.parse
import json
import threading

# ==========================================
# CONFIGURATION
# ==========================================
PORT = '/dev/serial0'
BAUD = 115200
MOONRAKER_URL = 'http://127.0.0.1:7125'

# Default Language Index: 5 = French, 2 = English, 3 = German, etc.
# Dynamically switchable at runtime from the screen without disk I/O.
LANGUAGE = 5

# Global State Variables
current_fan_speed = 0.0
led_status = False

# G-code File Management & Pagination
gcode_files = []           # List of relative file paths (.path)
selected_file_index = None  # Currently selected file index in gcode_files
FILES_PER_PAGE = 4         # Number of file rows displayed per page on screen
current_file_page = 0      # Current page index (0-indexed)

# Dynamic Preheat Presets (°C)
pla_nozzle_preset = 205
pla_bed_preset = 60
abs_nozzle_preset = 240
abs_bed_preset = 100

# Value cache to prevent unnecessary serial bus spam
last_sent_cache = {}

# Thread lock for serial port writes
serial_lock = threading.Lock()

print(f"Starting DWIN Touchscreen Bridge on {PORT} at {BAUD} baud...")
try:
    ser = serial.Serial(PORT, BAUD, timeout=0.1)
    ser.flushInput()
    ser.flushOutput()
except Exception as e:
    print(f"Error opening serial port {PORT}: {e}")
    exit(1)


# ==========================================
# HARDWARE USB MCU DETECTION
# ==========================================

def is_mcu_hardware_connected():
    """Checks if the printer mainboard (MCU) is physically powered on via USB."""
    try:
        # Check standard Linux USB serial symlinks and device nodes
        by_id = glob.glob('/dev/serial/by-id/*')
        if by_id:
            return True
        usb_nodes = glob.glob('/dev/ttyUSB*') + glob.glob('/dev/ttyACM*')
        return len(usb_nodes) > 0
    except Exception:
        return False


# ==========================================
# DWIN SERIAL PROTOCOL HELPERS
# ==========================================

def send_packet(packet):
    """Safely writes a raw byte packet to the DWIN LCD via UART."""
    with serial_lock:
        ser.write(packet)
        ser.flush()
    time.sleep(0.002)  # 2ms inter-frame delay is optimal for DWIN T5L ASIC

def write_vp_word(addr, val):
    """Writes a 16-bit unsigned integer to a specific VP address."""
    val = int(val) & 0xFFFF
    packet = bytes([0x5A, 0xA5, 0x05, 0x82, (addr >> 8) & 0xFF, addr & 0xFF, (val >> 8) & 0xFF, val & 0xFF])
    send_packet(packet)

def write_vp_word_cached(addr, val):
    """Writes a 16-bit word only if the value has changed since last transmission."""
    val = int(val) & 0xFFFF
    if last_sent_cache.get(addr) != val:
        last_sent_cache[addr] = val
        write_vp_word(addr, val)

def write_vp_dword(addr, val):
    """Writes a 32-bit unsigned integer to a specific VP address (used for page switching)."""
    val = int(val) & 0xFFFFFFFF
    packet = bytes([
        0x5A, 0xA5, 0x07, 0x82,
        (addr >> 8) & 0xFF, addr & 0xFF,
        (val >> 24) & 0xFF, (val >> 16) & 0xFF,
        (val >> 8) & 0xFF, val & 0xFF
    ])
    send_packet(packet)

def write_vp_string(addr, string_val):
    """Writes a fixed-width ASCII string to a VP address (max 20 characters per line)."""
    chars = string_val.encode('ascii', errors='ignore')
    if len(chars) > 20:
        chars = chars[:20]
    chars = chars.ljust(20, b' ')  # Pad with spaces to clear previous text
    length = 3 + len(chars)
    packet = bytes([0x5A, 0xA5, length, 0x82, (addr >> 8) & 0xFF, addr & 0xFF]) + chars
    send_packet(packet)

def apply_language(lang_id):
    """Updates all screen title VPs to the specified language index and sets radio buttons."""
    global LANGUAGE
    LANGUAGE = int(lang_id)
    print(f"Switching active language pack to Index = {LANGUAGE}")
    
    ranges = [
        (0x1300, 0x1387),
        (0x1400, 0x142D)
    ]
    for start, end in ranges:
        for addr in range(start, end):
            if 0x1411 <= addr <= 0x1419:
                continue
            write_vp_word(addr, LANGUAGE)
            
    # Update language radio button checkmarks
    for i in range(9):
        write_vp_word(0x1411 + i, 1 if (i == (LANGUAGE - 1)) else 0)
        
    write_vp_word(0x132D, LANGUAGE)
    write_vp_word(0x132E, LANGUAGE)


# ==========================================
# ASYNCHRONOUS MOONRAKER API DISPATCHERS
# ==========================================

def _send_gcode_worker(gcode_script):
    """Background worker for sending G-code commands to Moonraker."""
    print(f"Sending G-code: {gcode_script}")
    try:
        url = f"{MOONRAKER_URL}/printer/gcode/script"
        headers = {'Content-Type': 'application/json'}
        data = json.dumps({"script": gcode_script}).encode('utf-8')
        req = urllib.request.Request(url, data=data, headers=headers, method='POST')
        with urllib.request.urlopen(req, timeout=1):
            pass
    except Exception as e:
        print(f"Error sending G-code: {e}")

def send_gcode(gcode_script):
    """Dispatches a G-code script asynchronously to prevent blocking touch event processing."""
    threading.Thread(target=_send_gcode_worker, args=(gcode_script,), daemon=True).start()

def _trigger_print_start_worker(filename):
    """Background worker to start printing a G-code file via Moonraker."""
    print(f"Starting print job for file: {filename}")
    try:
        encoded_name = urllib.parse.quote(filename)
        url = f"{MOONRAKER_URL}/printer/print/start?filename={encoded_name}"
        req = urllib.request.Request(url, data=b'{}', headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=2):
            pass
    except Exception as e:
        print(f"Error starting print via API: {e}")
        # Fallback to SDCARD_PRINT_FILE G-code macro
        send_gcode(f'SDCARD_PRINT_FILE FILENAME="{filename}"')

def trigger_print_start(filename):
    """Starts printing a file asynchronously without blocking UI interactions."""
    threading.Thread(target=_trigger_print_start_worker, args=(filename,), daemon=True).start()

def render_file_list_page():
    """Renders the current 4 file items and updates the page counter on screen."""
    global gcode_files, current_file_page, selected_file_index
    
    max_pages = max(1, (len(gcode_files) + FILES_PER_PAGE - 1) // FILES_PER_PAGE)
    current_file_page = max(0, min(max_pages - 1, current_file_page))
    start_idx = current_file_page * FILES_PER_PAGE
    
    # Update page numbers at the bottom (Current Page / Total Pages)
    write_vp_word(0x10CA, current_file_page + 1)
    write_vp_word(0x10CC, max_pages)
    
    # Populate the 4 file text slots on the active page
    for slot in range(FILES_PER_PAGE):
        vp_addr = 0x200A + (slot * 0x14)
        file_idx = start_idx + slot
        if file_idx < len(gcode_files):
            full_path = gcode_files[file_idx]
            disp_name = full_path.split("/")[-1] if "/" in full_path else full_path
            write_vp_string(vp_addr, disp_name)
        else:
            write_vp_string(vp_addr, "")
            
        # Highlight icon if this file is selected
        is_selected = (selected_file_index == file_idx)
        write_vp_word(0x1221 + slot, 1 if is_selected else 0)
        
    # Clear remaining slots
    for slot in range(FILES_PER_PAGE, 20):
        vp_addr = 0x200A + (slot * 0x14)
        write_vp_string(vp_addr, "")
        write_vp_word(0x1221 + slot, 0)

def _update_file_list_worker():
    """Background worker to fetch G-code files from Moonraker and populate the list."""
    global gcode_files, selected_file_index, current_file_page
    try:
        url = f"{MOONRAKER_URL}/server/files/list?root=gcodes"
        req = urllib.request.Request(url, method='GET')
        with urllib.request.urlopen(req, timeout=2) as response:
            res = json.loads(response.read().decode('utf-8'))
            result = res.get("result", [])
            
            # Sort files by modification date (newest first)
            result.sort(key=lambda x: x.get("modified", 0), reverse=True)
            
            gcode_files = [f.get("path") for f in result if f.get("path", "").lower().endswith((".gcode", ".g"))]
            selected_file_index = None
            current_file_page = 0
            render_file_list_page()
    except Exception as e:
        print(f"Error fetching file list from Moonraker: {e}")

def update_file_list_from_moonraker():
    """Triggers an asynchronous file list refresh."""
    threading.Thread(target=_update_file_list_worker, daemon=True).start()

def trigger_klipper_restart():
    """Triggers a fast Klipper restart (RESTART)."""
    try:
        url = f"{MOONRAKER_URL}/printer/restart"
        req = urllib.request.Request(url, data=b'{}', headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=1):
            pass
    except Exception as e:
        pass

def query_moonraker_info():
    """Fetches global Klipper/Moonraker server state."""
    try:
        url = f"{MOONRAKER_URL}/printer/info"
        req = urllib.request.Request(url, method='GET')
        with urllib.request.urlopen(req, timeout=1) as response:
            res = json.loads(response.read().decode('utf-8'))
            return res.get("result", {})
    except Exception as e:
        return None

def query_moonraker():
    """Queries detailed printer status objects."""
    try:
        url = f"{MOONRAKER_URL}/printer/objects/query?extruder&heater_bed&toolhead&gcode_move&print_stats&fan&virtual_sdcard&display_status"
        req = urllib.request.Request(url, method='GET')
        with urllib.request.urlopen(req, timeout=1) as response:
            res = json.loads(response.read().decode('utf-8'))
            return res.get("result", {}).get("status", {})
    except Exception as e:
        return None


# ==========================================
# SCREEN INITIALIZATION
# ==========================================

def boot_sequence():
    """Executes the startup animation and initializes all graphics/presets."""
    global last_sent_cache
    print("Executing screen boot initialization...")
    last_sent_cache.clear()
    
    # 1. Show boot logo (Page 0)
    write_vp_dword(0x0084, 0x5A010000)
    time.sleep(0.2)
    
    # 2. Boot progress bar animation
    for progress in range(0, 101, 25):
        write_vp_word(0x1000, progress)
        time.sleep(0.03)
        
    # 3. Apply active language pack
    apply_language(LANGUAGE)
        
    # 4. Status icons and preheat presets
    write_vp_word(0x116B, 1)
    write_vp_word(0x116C, 1)
    write_vp_word(0x116D, 0)
    write_vp_word(0x116E, 0)
    write_vp_word(0x1200, 1)
    
    write_vp_word(0x1090, pla_nozzle_preset)
    write_vp_word(0x1092, pla_bed_preset)
    write_vp_word(0x1094, abs_nozzle_preset)
    write_vp_word(0x1096, abs_bed_preset)
    
    write_vp_word(0x100E, 0)
    write_vp_word(0x1016, 0)
    
    # Populate file list
    update_file_list_from_moonraker()
    
    # 5. Switch to Main Menu (Page 1)
    write_vp_dword(0x0084, 0x5A010001)
    print("Screen initialized successfully!")


# ==========================================
# TOUCH EVENT HANDLER
# ==========================================

def handle_screen_input(addr, val, words):
    """Dispatches touch events from the screen with low latency."""
    global current_fan_speed, led_status
    global pla_nozzle_preset, pla_bed_preset, abs_nozzle_preset, abs_bed_preset
    global gcode_files, selected_file_index, current_file_page

    print(f"Screen input detected: VP=0x{addr:04X}, Value={val} (Words={words})")

    # 1. Main Menu Navigation (MainEnterKey)
    if addr == 0x1002:
        if val == 1:
            write_vp_dword(0x0084, 0x5A010002)
            update_file_list_from_moonraker()
        elif val == 2:
            write_vp_dword(0x0084, 0x5A010010)
        elif val == 3:
            write_vp_dword(0x0084, 0x5A010015)
        elif val == 4:
            write_vp_dword(0x0084, 0x5A010019)
        elif val in [5, 8]:
            write_vp_dword(0x0084, 0x5A010001)
        elif val == 6:
            write_vp_dword(0x0084, 0x5A01001A)

    # 2. Prepare & Settings Navigation (PrepareEnterKey)
    elif addr == 0x103E:
        if val == 1:
            write_vp_dword(0x0084, 0x5A01001C)
        elif val == 2:
            write_vp_dword(0x0084, 0x5A010021)
        elif val == 3:
            write_vp_dword(0x0084, 0x5A010010)
        elif val == 5:
            write_vp_dword(0x0084, 0x5A010018)
        elif val == 6:
            send_gcode("M84")
            write_vp_word(0x1200, 1)
        elif val == 7:
            write_vp_dword(0x0084, 0x5A01002B)
        elif val == 8:
            write_vp_dword(0x0084, 0x5A010015)
        elif val == 9:
            write_vp_dword(0x0084, 0x5A010001)
        elif val == 10:
            write_vp_dword(0x0084, 0x5A01002A)
        elif val == 15:  # Language Back Arrow -> Return to Control/Prepare Menu
            write_vp_dword(0x0084, 0x5A010015)

    # 3. Temperature Control Menu (TempControlKey)
    elif addr == 0x1030:
        if val == 2:
            write_vp_dword(0x0084, 0x5A010014)
        elif val == 3:
            write_vp_word(0x1090, pla_nozzle_preset)
            write_vp_word(0x1092, pla_bed_preset)
            write_vp_word(0x132D, LANGUAGE)
            write_vp_dword(0x0084, 0x5A010016)
        elif val == 4:
            write_vp_word(0x1094, abs_nozzle_preset)
            write_vp_word(0x1096, abs_bed_preset)
            write_vp_word(0x132E, LANGUAGE)
            write_vp_dword(0x0084, 0x5A010017)
        elif val == 5:
            send_gcode(f"M104 S{pla_nozzle_preset}\nM140 S{pla_bed_preset}")
        elif val == 6:
            send_gcode(f"M104 S{abs_nozzle_preset}\nM140 S{abs_bed_preset}")
        elif val == 7:
            write_vp_dword(0x0084, 0x5A010015)
        elif val == 8:
            write_vp_dword(0x0084, 0x5A010014)

    # 4. Cooldown (CoolDownKey)
    elif addr == 0x1032:
        if val == 1:
            send_gcode("TURN_OFF_HEATERS")
        elif val == 2:
            write_vp_dword(0x0084, 0x5A010015)
        elif val == 4:
            write_vp_dword(0x0084, 0x5A010030)

    # 5. Adjustments & Peripheral Toggles (AdjustEnterKey)
    elif addr == 0x1004:
        if val == 1 or val == 7:
            write_vp_dword(0x0084, 0x5A01000E)
        elif val == 5:
            write_vp_dword(0x0084, 0x5A01000F)
        elif val == 2:
            write_vp_dword(0x0084, 0x5A01000A)
        elif val == 3:
            if current_fan_speed > 0.0:
                send_gcode("M106 S0")
                write_vp_word(0x101E, 1)
            else:
                send_gcode("M106 S255")
                write_vp_word(0x101E, 0)
        elif val == 4:
            led_status = not led_status
            send_gcode(f"SET_PIN PIN=led VALUE={1 if led_status else 0}")
            write_vp_word(0x101F, 1 if led_status else 0)

    # 6. Physical Actions & Homing
    elif addr == 0x1046:
        if val == 4:
            send_gcode("G28 X Y")
        elif val == 5:
            send_gcode("G28")
            write_vp_word(0x1200, 0)
        elif val == 1:
            write_vp_dword(0x0084, 0x5A010010)
        elif val == 2:
            write_vp_dword(0x0084, 0x5A010011)
        elif val == 3:
            write_vp_dword(0x0084, 0x5A010012)

    elif addr == 0x1034:
        send_gcode(f"M104 S{val}")
    elif addr == 0x103A:
        send_gcode(f"M140 S{val}")

    # Preheat Presets
    elif addr == 0x1090:
        pla_nozzle_preset = val
        write_vp_word(0x1090, val)
    elif addr == 0x1092:
        pla_bed_preset = val
        write_vp_word(0x1092, val)
    elif addr == 0x1094:
        abs_nozzle_preset = val
        write_vp_word(0x1094, val)
    elif addr == 0x1096:
        abs_bed_preset = val
        write_vp_word(0x1096, val)

    # Axis Coordinates
    elif addr == 0x1048:
        target = val / 10.0
        send_gcode(f"G90\nG1 X{target:.1f} F3000")
        write_vp_word(0x1200, 0)
    elif addr == 0x104A:
        target = val / 10.0
        send_gcode(f"G90\nG1 Y{target:.1f} F3000")
        write_vp_word(0x1200, 0)
    elif addr == 0x104C:
        target = val / 10.0
        send_gcode(f"G90\nG1 Z{target:.1f} F600")
        write_vp_word(0x1200, 0)

    # Direct Filament Load / Unload
    elif addr == 0x1052:
        dist = val / 10.0
        write_vp_word(0x1052, val)
        write_vp_dword(0x0084, 0x5A010013)
        send_gcode(f"M109 S200\nG91\nG1 E{dist:.1f} F120\nG90")
        
    elif addr == 0x1054:
        dist = val / 10.0
        write_vp_word(0x1054, val)
        write_vp_dword(0x0084, 0x5A010013)
        send_gcode(f"M109 S200\nG91\nG1 E-{dist:.1f} F240\nG90")
        
    elif addr == 0x1056 and val == 3:
        write_vp_dword(0x0084, 0x5A010013)
        send_gcode("M104 S0")

    # Dynamic Language Selection
    elif addr == 0x105C:
        if val > 0:
            apply_language(val)

    # G-code File Selection (SelectFileKey)
    elif addr == 0x2199:
        slot_idx = val - 1
        actual_file_idx = (current_file_page * FILES_PER_PAGE) + slot_idx
        if 0 <= actual_file_idx < len(gcode_files):
            selected_file_index = actual_file_idx
            selected_filename = gcode_files[actual_file_idx]
            render_file_list_page()
            short_name = selected_filename.split("/")[-1] if "/" in selected_filename else selected_filename
            write_vp_string(0x219A, short_name[:20])

    # Start Print & File Pagination (StartFileKey)
    elif addr == 0x2198:
        if val == 1:  # Start Print / Resume
            if selected_file_index is not None and 0 <= selected_file_index < len(gcode_files):
                file_to_print = gcode_files[selected_file_index]
                trigger_print_start(file_to_print)
            else:
                send_gcode("RESUME")
        elif val == 2:  # Next Page
            max_pages = max(1, (len(gcode_files) + FILES_PER_PAGE - 1) // FILES_PER_PAGE)
            if current_file_page < max_pages - 1:
                current_file_page += 1
                render_file_list_page()
        elif val == 3:  # Previous Page
            if current_file_page > 0:
                current_file_page -= 1
                render_file_list_page()
        elif val == 4:  # First Page
            current_file_page = 0
            render_file_list_page()
        elif val == 5:  # Last Page
            max_pages = max(1, (len(gcode_files) + FILES_PER_PAGE - 1) // FILES_PER_PAGE)
            current_file_page = max_pages - 1
            render_file_list_page()

    # Print Job Control (Cancel, Pause, Resume)
    elif addr == 0x1008:
        send_gcode("CANCEL_PRINT")
    elif addr == 0x100A:
        send_gcode("PAUSE")
    elif addr == 0x100C:
        send_gcode("RESUME")

    # Bed Mesh Calibration
    elif addr == 0x1044:
        if val == 6:
            send_gcode("BED_MESH_CALIBRATE")


# ==========================================
# THREAD WORKERS
# ==========================================

def serial_listener():
    """Listens continuously for incoming UART packets from the screen."""
    buffer = bytearray()
    while True:
        try:
            available = ser.in_waiting
            if available > 0:
                chunk = ser.read(available)
                buffer.extend(chunk)
                while len(buffer) >= 2:
                    start_idx = buffer.find(b'\x5a\xa5')
                    if start_idx != -1:
                        if start_idx > 0:
                            del buffer[:start_idx]
                        if len(buffer) >= 3:
                            length = buffer[2]
                            if len(buffer) >= length + 3:
                                packet = bytes(buffer[:length + 3])
                                del buffer[:length + 3]
                                
                                command = packet[3]
                                if command == 0x83:
                                    addr = (packet[4] << 8) | packet[5]
                                    payload = packet[7:]
                                    words = []
                                    for i in range(0, len(payload), 2):
                                        if i + 1 < len(payload):
                                            words.append((payload[i] << 8) | payload[i+1])
                                    if words:
                                        handle_screen_input(addr, words[0], words)
                            else:
                                break
                        else:
                            break
                    else:
                        if len(buffer) > 256:
                            del buffer[:-1]
                        break
            else:
                time.sleep(0.005)
        except Exception as e:
            print(f"Serial thread exception: {e}")
            time.sleep(0.1)

def status_poller():
    """Polls Klipper state and updates screen indicators with delta caching and smart USB detection."""
    global current_fan_speed
    last_state = None
    is_connected_to_mcu = False
    last_mcu_present = False
    
    while True:
        mcu_present = is_mcu_hardware_connected()
        
        # Trigger Klipper restart ONLY ONCE when the MCU USB hardware device is powered on
        if mcu_present and not last_mcu_present:
            print("Printer USB hardware detected! Sending one-time Klipper restart...")
            time.sleep(1.0)  # Allow MCU bootloader to finish initialization
            trigger_klipper_restart()
            last_mcu_present = True
        elif not mcu_present:
            last_mcu_present = False

        # If MCU hardware is physically not present (printer powered off), skip Moonraker queries
        if not mcu_present:
            if is_connected_to_mcu:
                print("Printer USB disconnected / powered off.")
                is_connected_to_mcu = False
            time.sleep(1.0)
            continue

        info = query_moonraker_info()
        state = info.get("state", "offline") if info else "offline"
        
        if state == "ready":
            if not is_connected_to_mcu:
                print("Printer MCU connected and READY! Initializing screen...")
                boot_sequence()
                is_connected_to_mcu = True

            status = query_moonraker()
            if status:
                try:
                    ext_temp = status.get('extruder', {}).get('temperature', 0)
                    ext_target = status.get('extruder', {}).get('target', 0)
                    bed_temp = status.get('heater_bed', {}).get('temperature', 0)
                    bed_target = status.get('heater_bed', {}).get('target', 0)
                    
                    gcode_pos = status.get('gcode_move', {}).get('gcode_position', [0.0, 0.0, 0.0])
                    speed_factor = status.get('gcode_move', {}).get('speed_factor', 1.0)
                    
                    fan_speed = status.get('fan', {}).get('speed', 0.0)
                    current_fan_speed = fan_speed
                    
                    print_stats = status.get('print_stats', {})
                    p_state = print_stats.get('state', 'standby')
                    duration = print_stats.get('print_duration', 0.0)
                    filename = print_stats.get('filename', '')

                    # Progress calculation
                    v_sd = status.get('virtual_sdcard', {})
                    v_progress = v_sd.get('progress', None)
                    d_stat = status.get('display_status', {})
                    d_progress = d_stat.get('progress', None)
                    p_progress = print_stats.get('progress', None)
                    
                    progress = 0.0
                    for p in [v_progress, d_progress, p_progress]:
                        if p is not None and p > 0:
                            progress = p
                            break

                    # Delta-cached UI updates
                    write_vp_word_cached(0x1036, int(ext_temp))
                    write_vp_word_cached(0x1034, int(ext_target))
                    write_vp_word_cached(0x103C, int(bed_temp))
                    write_vp_word_cached(0x103A, int(bed_target))
                    
                    write_vp_word_cached(0x1048, int(gcode_pos[0] * 10))
                    write_vp_word_cached(0x104A, int(gcode_pos[1] * 10))
                    write_vp_word_cached(0x104C, int(gcode_pos[2] * 10))

                    write_vp_word_cached(0x101E, 1 if current_fan_speed == 0.0 else 0)

                    # State transitions
                    if p_state != last_state:
                        print(f"Klipper state change: {last_state} -> {p_state}")
                        if p_state == "printing":
                            write_vp_dword(0x0084, 0x5A01000A)
                            write_vp_string(0x21C0, filename)
                        elif p_state == "paused":
                            write_vp_dword(0x0084, 0x5A01000C)
                        elif p_state in ["standby", "complete", "cancelled"] and last_state == "printing":
                            write_vp_dword(0x0084, 0x5A010001)
                        last_state = p_state

                    if p_state in ["printing", "paused"]:
                        percent = max(0, min(100, int(progress * 100)))
                        
                        write_vp_word_cached(0x1006, int(speed_factor * 100))
                        write_vp_word_cached(0x100E, percent)
                        write_vp_word_cached(0x1016, percent)
                        
                        write_vp_word_cached(0x1010, int(duration // 3600))
                        write_vp_word_cached(0x1012, int((duration % 3600) // 60))
                        
                        if progress > 0.01:
                            remaining = int((duration / progress) - duration)
                            write_vp_word_cached(0x10D2, int(remaining // 3600))
                            write_vp_word_cached(0x10D4, int((remaining % 3600) // 60))
                        else:
                            write_vp_word_cached(0x10D2, 0)
                            write_vp_word_cached(0x10D4, 0)

                except Exception as e:
                    print(f"Error updating DWIN status: {e}")
        else:
            if is_connected_to_mcu:
                print(f"Printer disconnected / powered off (State: {state}).")
                is_connected_to_mcu = False

        time.sleep(0.5)


# ==========================================
# APPLICATION ENTRYPOINT
# ==========================================

t_list = threading.Thread(target=serial_listener, daemon=True)
t_poll = threading.Thread(target=status_poller, daemon=True)

t_list.start()
t_poll.start()

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    print("\nExiting DWIN bridge.")
    ser.close()