import asyncio
import logging
from typing import Callable, Awaitable, Optional

log = logging.getLogger("mavlink_watcher")

MAV_MODE_FLAG_SAFETY_ARMED = 128
AUTOPILOT_COMPONENT_ID = 1


async def watch_arm_state(
    on_arm: Callable[[str], Awaitable[None]],
    on_disarm: Callable[[str], Awaitable[None]],
    port: int = 14550,
    poll_interval: float = 0.2,
):
    """
    Listen on UDP :14550 for ArduSub HEARTBEAT messages.
    Calls on_arm("mavlink") when the armed flag transitions low→high.
    Calls on_disarm("mavlink") when it transitions high→low.

    Filters to component ID 1 (autopilot) to ignore GCS/BlueOS heartbeats.
    """
    # Import here so the module can be imported without pymavlink installed
    try:
        from pymavlink import mavutil
    except ImportError:
        log.error("pymavlink not installed — MAVLink watcher disabled")
        return

    log.info(f"Connecting to MAVLink on UDP :{port}")
    conn = mavutil.mavlink_connection(f"udpin:0.0.0.0:{port}")
    armed = False

    while True:
        try:
            msg = conn.recv_match(type="HEARTBEAT", blocking=False)
            if msg and msg.get_srcComponent() == AUTOPILOT_COMPONENT_ID:
                is_armed = bool(msg.base_mode & MAV_MODE_FLAG_SAFETY_ARMED)

                if is_armed and not armed:
                    armed = True
                    log.info("MAVLink: ARMED — starting recording")
                    await on_arm("mavlink")

                elif not is_armed and armed:
                    armed = False
                    log.info("MAVLink: DISARMED — stopping recording")
                    await on_disarm("mavlink")

        except Exception as e:
            log.warning(f"MAVLink recv error: {e}")

        await asyncio.sleep(poll_interval)
