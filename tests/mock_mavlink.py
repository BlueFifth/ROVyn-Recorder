"""
Mock MAVLink heartbeat sender for Phase 1/2 testing.
Toggles armed/disarmed every 15 seconds.
Run alongside the recorder to test arm/disarm state transitions.

Usage: python3 tests/mock_mavlink.py
"""
import socket
import struct
import time

MAV_TYPE_SUBMARINE        = 12
MAV_AUTOPILOT_ARDUPILOTMEGA = 3
MAV_MODE_FLAG_SAFETY_ARMED  = 128
MAV_STATE_ACTIVE            = 4
MAV_STATE_STANDBY           = 3

TARGET_IP   = "127.0.0.1"
TARGET_PORT = 14550


def make_heartbeat(armed: bool) -> bytes:
    """
    Build a minimal MAVLink v1 HEARTBEAT packet.
    Message ID 0 = HEARTBEAT.
    """
    base_mode = MAV_MODE_FLAG_SAFETY_ARMED if armed else 0
    custom_mode = 0
    system_status = MAV_STATE_ACTIVE if armed else MAV_STATE_STANDBY

    payload = struct.pack(
        "<IBBBBB",
        custom_mode,           # uint32
        MAV_TYPE_SUBMARINE,    # uint8
        MAV_AUTOPILOT_ARDUPILOTMEGA,
        base_mode,
        system_status,
        3,                     # mavlink version
    )

    length   = len(payload)
    seq      = 0
    sys_id   = 1
    comp_id  = 1   # autopilot component
    msg_id   = 0   # HEARTBEAT

    header  = struct.pack("BBBBBB", 0xFE, length, seq, sys_id, comp_id, msg_id)
    packet  = header + payload

    # Simple CRC (not real MAVLink CRC — sufficient for pymavlink recv_match test)
    crc = 0xFFFF
    packet += struct.pack("<H", crc)
    return packet


def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    armed = False
    print(f"Sending heartbeats to {TARGET_IP}:{TARGET_PORT}")
    print("Toggling armed state every 15 seconds. Ctrl+C to stop.")

    seq = 0
    while True:
        pkt = make_heartbeat(armed)
        sock.sendto(pkt, (TARGET_IP, TARGET_PORT))
        state = "ARMED" if armed else "DISARMED"
        print(f"[{time.strftime('%H:%M:%S')}] Heartbeat sent — {state}")

        time.sleep(1)
        seq += 1

        # Toggle every 15 heartbeats
        if seq % 15 == 0:
            armed = not armed
            print(f"\n>>> Toggling to {'ARMED' if armed else 'DISARMED'} <<<\n")


if __name__ == "__main__":
    main()
