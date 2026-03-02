import asyncio
import contextlib
import json
import re
import signal
import time
from typing import Dict, Optional, Set, Tuple

from bleak import BleakClient, BleakError, BleakScanner
from websockets import serve  

# ========= настройки / UUID =========
DEVICE_NAME_SUBSTR = "T-Watch-IMU"

SVC_UUID = "6f2d5d52-0f4d-4b2a-a02b-5a7a5b3a0e11"
TX_UUID  = "f5c8b9d0-3a5d-4d9d-9d67-2d7f1b9e4b22"  
RX_UUID  = "e8b6a830-8f6b-4d9c-a71c-6d8c2a3a5f33"  

WS_HOST = "127.0.0.1"
WS_PORT = 8765

# Протокол
MAGIC_FRAG  = 0xB1F1
MAGIC_FRAME = 0xB10F
FRAME_HDR_SIZE = 32
FRAG_HDR_SIZE  = 10  # 2 magic + 1 ver + 1 part_idx + 4 seq + 2 frag_len

# Реассамблер housekeeping
REASM_TTL_SEC  = 10.0
CLEAN_PERIOD_S = 2.0

VERBOSE = True
def vlog(*a, **k):
    if VERBOSE:
        print(*a, **k)

# ========= CRC32 =========
import zlib
def crc32_le_ieee(data: bytes) -> int:
    # тот же алгоритм, что в прошивке: init=0xFFFFFFFF, финальный инверт
    return zlib.crc32(data) & 0xFFFFFFFF

# ========= WS-хаб =========
class Hub:
    def __init__(self):
        self.json_clients: Set = set()
        self.bin_clients: Set = set()
        self._lock = asyncio.Lock()

    async def add_json(self, ws):
        async with self._lock:
            self.json_clients.add(ws)
            print(f"[WS] +json client. total={len(self.json_clients)}")


    async def add_bin(self, ws):
        async with self._lock:
            self.bin_clients.add(ws)

    async def remove(self, ws):
        async with self._lock:
            was_json = ws in self.json_clients
            self.json_clients.discard(ws)
            self.bin_clients.discard(ws)
            if was_json:
                print(f"[WS] -json client. total={len(self.json_clients)}")

    async def broadcast_json(self, obj: dict):
        data = json.dumps(obj)
        async with self._lock:
            targets = list(self.json_clients)
        if not targets:
            print("[WS] broadcast_json: no clients")
            return
        sent = 0
        for ws in targets:
            try:
                await ws.send(data); sent += 1 
            except Exception:
                pass
        print(f"[WS] broadcast_json: sent={sent}, size={len(data)}")

    async def broadcast_bin(self, data: bytes):
        async with self._lock:
            dead = []
            for ws in list(self.bin_clients):
                try:
                    await ws.send(data)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self.bin_clients.discard(ws)

hub = Hub()

# ========= WS-обработчики =========
async def ws_handler_json(ws):
    await hub.add_json(ws)
    try:
        async for _ in ws:
            pass
    finally:
        await hub.remove(ws)

async def ws_handler_bin(ws):
    await hub.add_bin(ws)
    try:
        async for _ in ws:
            pass
    finally:
        await hub.remove(ws)

async def ws_router(ws):
    raw_path = getattr(ws, "path", "/")
    try:
        print(f"[WS] incoming path={raw_path!r} from={ws.remote_address} "
              f"headers={{'Host':{ws.request_headers.get('Host')!r}, "
              f"'Origin':{ws.request_headers.get('Origin')!r}}}")
    except Exception:
        print(f"[WS] incoming path={raw_path!r}")

    path = raw_path.split('?', 1)[0].rstrip('/') or '/'

    if path in ("/ws/json", "/"):
        if path == "/":
            print("[WS] WARNING: client connected to '/', treating as /ws/json")
        print("[WS] connected /ws/json")
        await ws_handler_json(ws)
    elif path == "/ws/bin":
        print("[WS] connected /ws/bin")
        await ws_handler_bin(ws)
    else:
        reason = "use /ws/json or /ws/bin"
        print(f"[WS] closing unknown path={raw_path!r} → {reason}")
        await ws.close(code=1008, reason=reason)

# ========= Реассамблер =========
class ReasmState:
    __slots__ = ("parts", "got", "buf", "seen", "last_touch", "dev_id")
    def __init__(self, total_len: int, parts: int, hdr: bytes, first_body_tail: bytes):
        self.parts = parts
        self.buf = bytearray(total_len)
        self.buf[:FRAME_HDR_SIZE] = hdr
        self.buf[FRAME_HDR_SIZE:FRAME_HDR_SIZE+len(first_body_tail)] = first_body_tail
        self.got = FRAME_HDR_SIZE + len(first_body_tail)
        self.seen: Set[int] = {0}
        self.last_touch = time.monotonic()
        self.dev_id = int.from_bytes(hdr[4:8], "little")  # Extract dev_id from header
        vlog(f"ReasmState: created with total_len={total_len} parts={parts} hdr={len(hdr)} tail={len(first_body_tail)} got={self.got} dev_id={self.dev_id}")

class Assembler:
    def __init__(self):
        self._states: Dict[Tuple[int, int], ReasmState] = {}  # (dev_id, seq) -> ReasmState
        self._seq_to_dev_id: Dict[int, int] = {}  # seq -> dev_id mapping for active reassemblies
        self._lock = asyncio.Lock()
        self._stats = {"total_frags": 0, "total_frames": 0, "last_frame_time": 0}

    async def cleanup_loop(self):
        while True:
            await asyncio.sleep(CLEAN_PERIOD_S)
            now = time.monotonic()
            async with self._lock:
                stale = [(dev_id, seq) for (dev_id, seq), st in self._states.items() if now - st.last_touch > REASM_TTL_SEC]
                for dev_id, seq in stale:
                    vlog(f"reasm: drop stale dev_id={dev_id} seq={seq}")
                    self._states.pop((dev_id, seq), None)
                    self._seq_to_dev_id.pop(seq, None)

            # Статистика
            vlog(f"reasm: stats - frags={self._stats['total_frags']} frames={self._stats['total_frames']} last_frame={now - self._stats['last_frame_time']:.1f}s ago")

    async def on_fragment(self, frag: bytes):
        if len(frag) < FRAG_HDR_SIZE:
            vlog("frag: too short", len(frag)); return

        magic    = int.from_bytes(frag[0:2], "little")
        ver      = frag[2]
        part_idx = frag[3]
        seq      = int.from_bytes(frag[4:8], "little", signed=False)
        frag_len = int.from_bytes(frag[8:10], "little")
        body     = frag[10:]

        vlog(f"frag: magic=0x{magic:04X} ver={ver} idx={part_idx} seq={seq} len={frag_len} body={len(body)}")
        vlog(f"frag: raw_frag_size={len(frag)} FRAG_HDR_SIZE={FRAG_HDR_SIZE} expected_body={len(frag)-FRAG_HDR_SIZE}")
        if len(frag) > FRAG_HDR_SIZE:
            vlog(f"frag: first_body_bytes={frag[FRAG_HDR_SIZE:FRAG_HDR_SIZE+min(8, len(frag)-FRAG_HDR_SIZE)].hex()}")

        if magic != MAGIC_FRAG:
            return
        if frag_len != len(body):
            vlog(f"frag: len mismatch seq={seq} idx={part_idx} frag_len={frag_len} body={len(body)}")
            return
        if frag_len == 0:
            vlog(f"frag: zero length for seq={seq} idx={part_idx}")
            return
            
        if part_idx > 0 and len(body) == 0:
            vlog(f"frag: empty body for non-zero part_idx={part_idx} seq={seq}")
            return

        # Обновляем статистику
        self._stats["total_frags"] += 1

        if part_idx == 0:
            if len(body) < FRAME_HDR_SIZE:
                vlog("frag0: body < FRAME_HDR_SIZE"); return
            hdr = body[:FRAME_HDR_SIZE]
            if int.from_bytes(hdr[0:2], "little") != MAGIC_FRAME:
                vlog("frag0: bad frame magic"); return
            parts       = hdr[27]
            payload_len = int.from_bytes(hdr[28:32], "little")
            total       = FRAME_HDR_SIZE + payload_len + 4
            tail        = body[FRAME_HDR_SIZE:]
            vlog(f"frag0: hdr_size={FRAME_HDR_SIZE} payload_len={payload_len} total={total} tail_len={len(tail)}")
            async with self._lock:
                state = ReasmState(total, parts, hdr, tail)
                self._states[(state.dev_id, seq)] = state
                self._seq_to_dev_id[seq] = state.dev_id
            vlog(f"frag0: dev_id={state.dev_id} seq={seq} parts={parts} total={total} first_tail={len(tail)} buf_size={len(self._states[(state.dev_id, seq)].buf)}")
            return
        else:
            if len(body) < frag_len:
                vlog(f"frag: body < frag_len seq={seq} idx={part_idx} frag_len={frag_len} body={len(body)}")
                return

        async with self._lock:
            dev_id = self._seq_to_dev_id.get(seq)
            if dev_id is None:
                vlog(f"frag: no dev_id mapping for seq={seq} (idx={part_idx})"); return

            key = (dev_id, seq)
            st = self._states.get(key)
            if not st:
                vlog(f"frag: no state for dev_id={dev_id} seq={seq} (idx={part_idx})"); return
            vlog(f"frag+: dev_id={dev_id} seq={seq} idx={part_idx} cur={st.got}/{len(st.buf)} add={len(body)} seen={sorted(st.seen)}")

            if part_idx in st.seen:
                vlog(f"frag: duplicate part_idx={part_idx} for dev_id={dev_id} seq={seq}")
                return
            end = st.got + len(body)
            vlog(f"frag+: dev_id={dev_id} seq={seq} idx={part_idx} body_size={len(body)} cursor={st.got} end={end} buf_size={len(st.buf)}")
            if end > len(st.buf):
                vlog(f"frag overflow: dev_id={dev_id} seq={seq} got={end}/{len(st.buf)} (idx={part_idx})")
                self._states.pop(key, None)
                self._seq_to_dev_id.pop(seq, None)
                return
            st.buf[st.got:end] = body
            st.got = end
            st.seen.add(part_idx)
            st.last_touch = time.monotonic()
            vlog(f"frag+: dev_id={dev_id} seq={seq} idx={part_idx} progress={st.got}/{len(st.buf)} parts_needed={st.parts}")
            if st.got != len(st.buf):
                return
            frame = bytes(st.buf)
            self._states.pop(key, None)
            self._seq_to_dev_id.pop(seq, None)

        completed_dev_id = int.from_bytes(frame[4:8], "little")
        vlog(f"frame done: dev_id={completed_dev_id} seq={seq} total={len(frame)}")

        self._stats["total_frames"] += 1
        self._stats["last_frame_time"] = time.monotonic()

        try:
            obj = frame_to_json(frame)
            await hub.broadcast_json(obj)
            vlog(f"frame: JSON sent successfully, seq={seq}")
        except Exception as e:
            vlog(f"frame: ERROR processing seq={seq}: {e}")
            try:
                await hub.broadcast_bin(frame)
                vlog(f"frame: Raw data sent as fallback, seq={seq}")
            except Exception as e2:
                vlog(f"frame: CRITICAL ERROR sending raw data seq={seq}: {e2}")

def frame_to_json(buf: bytes) -> dict:
    if len(buf) < FRAME_HDR_SIZE + 4:
        raise ValueError("frame too small")
    hdr = buf[:FRAME_HDR_SIZE]
    if int.from_bytes(hdr[0:2], "little") != MAGIC_FRAME:
        raise ValueError("bad MAGIC_FRAME")
    payload_len = int.from_bytes(hdr[28:32], "little")
    n           = int.from_bytes(hdr[22:24], "little")
    axes        = hdr[24]
    if axes != 0b111:
        raise ValueError("unsupported axes")
    if payload_len != 3 * n * 2:
        raise ValueError("payload_len mismatch")
    if len(buf) != FRAME_HDR_SIZE + payload_len + 4:
        raise ValueError("total length mismatch")

    data   = buf[:FRAME_HDR_SIZE] + buf[FRAME_HDR_SIZE:-4]
    stored = int.from_bytes(buf[-4:], "little")
    calc   = crc32_le_ieee(data)
    if stored != calc:
        raise ValueError(f"CRC mismatch: stored=0x{stored:08X} calc=0x{calc:08X}")

    dev_id = int.from_bytes(hdr[4:8], "little")
    seq    = int.from_bytes(hdr[8:12], "little")
    ts0_ns = int.from_bytes(hdr[12:20], "little")
    fs_hz  = int.from_bytes(hdr[20:22], "little")
    batt   = hdr[25]

    mv = memoryview(buf)[FRAME_HDR_SIZE:-4]
    ax = [0]*n; ay = [0]*n; az = [0]*n
    off = 0
    for i in range(n):
        ax[i] = int.from_bytes(mv[off:off+2], "little", signed=True); off += 2
    for i in range(n):
        ay[i] = int.from_bytes(mv[off:off+2], "little", signed=True); off += 2
    for i in range(n):
        az[i] = int.from_bytes(mv[off:off+2], "little", signed=True); off += 2

    return {
        "type": "imu",
        "meta": {"dev_id": dev_id, "seq": seq, "ts0_ns": ts0_ns,
                "fs_hz": fs_hz, "n": n, "axes": axes, "batt": batt},
        "ax": ax, "ay": ay, "az": az
    }

# ========= BLE discovery =========
def _name_of(dev, ad):
    return (getattr(ad, "local_name", None)
            or getattr(dev, "name", None)
            or "")

async def discover_candidates(timeout_s: float = 6.0):
    print("scan: ищу устройства…")
    seen = {}
    def cb(d, ad):
        try:
            uuids = {u.lower() for u in (ad.service_uuids or [])}
        except Exception:
            uuids = set()
        name = _name_of(d, ad)
        if (SVC_UUID in uuids) or (DEVICE_NAME_SUBSTR in name):
            seen[d.address] = (d, ad)

    scanner = BleakScanner(cb)
    await scanner.start()
    await asyncio.sleep(timeout_s)
    await scanner.stop()

    if not seen:
        print("scan: подходящих устройств не найдено")
    else:
        for addr, (d, ad) in seen.items():
            try:
                uuids = {u.lower() for u in (ad.service_uuids or [])}
                rssi = getattr(ad, "rssi", None)
            except Exception:
                uuids, rssi = set(), None
            print(f"scan: кандидат {addr} name='{_name_of(d, ad)}' RSSI={rssi} uuids={uuids}")
    return list(seen.values())

async def try_write(cli, uuid: str, data: bytes) -> bool:
    try:
        await cli.write_gatt_char(uuid, data, response=True)
        return True
    except Exception:
        try:
            await cli.write_gatt_char(uuid, data, response=False)
            return True
        except Exception as e2:
            print("write failed:", e2)
            return False

# ========= Сбор текстовых нотификаций + ожидание ACK =========
class TextFeed:
    def __init__(self):
        self._q: asyncio.Queue[str] = asyncio.Queue()

    def on_notify(self, data: bytes):
        if 1 <= len(data) <= 64 and all(32 <= b < 127 for b in data):
            try:
                s = data.decode("utf-8", "ignore")
                self._q.put_nowait(s)
            except Exception:
                pass

    async def wait_for(self, pattern: str, timeout: float) -> Optional[str]:
        deadline = time.monotonic() + timeout
        prog = re.compile(pattern)
        while True:
            tleft = deadline - time.monotonic()
            if tleft <= 0:
                return None
            try:
                s = await asyncio.wait_for(self._q.get(), timeout=tleft)
            except asyncio.TimeoutError:
                return None
            finally:
                with contextlib.suppress(Exception):
                    self._q.task_done()
            if prog.search(s):
                return s

# ========= BLE main connect/stream =========
async def try_connect_and_stream(dev, ad, asm: Assembler, on_connected=None, on_disconnected=None):
    name = _name_of(dev, ad)
    print(f"connect → {dev.address} ({name})")

    notify_q: asyncio.Queue[bytes] = asyncio.Queue()
    text_feed = TextFeed()
    last_notify_time = time.monotonic()
    notify_count = 0

    def on_notify(_char, data: bytes):
        nonlocal last_notify_time, notify_count
        last_notify_time = time.monotonic()
        notify_count += 1
        
        text_feed.on_notify(data)
        try:
            notify_q.put_nowait(data)
        except Exception:
            pass

    async def notify_loop():
        notify_cnt = 0
        while True:
            data = await notify_q.get()
            try:
                notify_cnt += 1

                if len(data) <= 32 and all(32 <= b < 127 for b in data):
                    s = data.decode("utf-8", "ignore")
                    print("notify(text):", s)

                if notify_cnt % 10 == 1:
                    print(f"notify #{notify_cnt}, {len(data)} bytes")

                if len(data) >= FRAG_HDR_SIZE:
                    await asm.on_fragment(data)
                else:
                    pass
            finally:
                notify_q.task_done()


    try:
        async with BleakClient(dev, timeout=10.0) as cli:
            svcs = await cli.get_services()
            want_tx = TX_UUID.lower()
            want_rx = RX_UUID.lower()
            have_tx = any(ch.uuid.lower() == want_tx for s in svcs for ch in s.characteristics)
            have_rx = any(ch.uuid.lower() == want_rx for s in svcs for ch in s.characteristics)
            print(f"svc check: TX={have_tx} RX={have_rx}")
            if not (have_tx and have_rx):
                print("skip: у устройства нет нужных характеристик TX/RX")
                return False

            await cli.start_notify(TX_UUID, on_notify)
            notify_task = asyncio.create_task(notify_loop())

            # ---------- Рукопожатие ----------
            # 1) TIME:<unix>
            await asyncio.sleep(0.2)
            await try_write(cli, RX_UUID, f"TIME:{int(time.time())}".encode())

            # Ждём ACK:TIME или STATE:SYNC=1
            ack_time = await text_feed.wait_for(r"(ACK:TIME|STATE:.*SYNC=1)", timeout=2.0)
            if ack_time:
                vlog("handshake: time synced:", ack_time)
            else:
                vlog("handshake: no ACK:TIME, продолжаем вслепую")

            # 2) STATE? (диагностика)
            await try_write(cli, RX_UUID, b"STATE?")
            _state = await text_feed.wait_for(r"^STATE:", timeout=1.0)
            if _state:
                print(_state)

            # 3) START (до трёх попыток)
            started = False
            for attempt in range(3):
                await try_write(cli, RX_UUID, b"START")
                got = await text_feed.wait_for(r"(ACK:START|STATE:.*ACTIVE=1)", timeout=1.5)
                if got:
                    vlog("handshake: started:", got)
                    started = True
                    break
                else:
                    vlog(f"handshake: START no ack, retry {attempt+1}/3")

            if not started:
                print("WARN: не удалось получить ACK:START — но слушаем поток дальше")

            print(f"streaming → ws://{WS_HOST}:{WS_PORT}/ws/json (и /ws/bin)")

            # Notify that we're connected
            if on_connected:
                await on_connected(dev.address)

            # Основной цикл: ждём разрыва + heartbeat
            while True:
                connected_attr = getattr(cli, "is_connected", False)
                connected = bool(connected_attr() if callable(connected_attr) else connected_attr)
                if not connected:
                    break
                
                # Heartbeat: проверяем, что нотификации приходят
                now = time.monotonic()
                if now - last_notify_time > 5.0:  # 5 секунд без нотификаций
                    print(f"WARN: No notifications for {now - last_notify_time:.1f}s, notify_count={notify_count}")
                    # Попробуем отправить PING для проверки соединения
                    await try_write(cli, RX_UUID, b"PING")
                    last_notify_time = now  # Сброс таймера
                
                await asyncio.sleep(0.5)

            notify_task.cancel()
            with contextlib.suppress(Exception):
                await notify_task

            # Notify that we're disconnected
            if on_disconnected:
                await on_disconnected(dev.address)

            return True

    except BleakError as e:
        print("BLE error:", e)
        # Notify that we're disconnected on error
        if on_disconnected:
            await on_disconnected(dev.address)
    except Exception as e:
        print("err:", e)
        # Notify that we're disconnected on error
        if on_disconnected:
            await on_disconnected(dev.address)
    return False

# ========= Device Manager =========
class DeviceManager:
    def __init__(self, max_devices: int = 4):
        self.max_devices = max_devices
        self.active_connections: Dict[str, asyncio.Task] = {}  # address -> connection_task
        self.connected_devices: Set[str] = set()  # addresses of connected devices
        self._lock = asyncio.Lock()

    async def add_device(self, dev, ad, asm: Assembler) -> bool:
        """Try to connect to a device if we haven't reached the limit"""
        address = dev.address

        async with self._lock:
            if address in self.connected_devices:
                vlog(f"Device {address} already connected")
                return False

            if len(self.connected_devices) >= self.max_devices:
                vlog(f"Max devices ({self.max_devices}) reached, cannot add {address}")
                return False

            # Start connection task
            task = asyncio.create_task(self._device_connection_loop(dev, ad, asm))
            self.active_connections[address] = task
            return True

    async def remove_device(self, address: str):
        """Remove a device from active connections"""
        async with self._lock:
            if address in self.active_connections:
                task = self.active_connections[address]
                task.cancel()
                with contextlib.suppress(Exception):
                    await task
                self.active_connections.pop(address, None)
            self.connected_devices.discard(address)
            vlog(f"Device {address} removed. Active devices: {len(self.connected_devices)}")

    async def _device_connection_loop(self, dev, ad, asm: Assembler):
        """Connection loop for a single device with reconnection logic"""
        address = dev.address
        name = _name_of(dev, ad)

        async def on_connected(addr):
            async with self._lock:
                self.connected_devices.add(addr)
            vlog(f"Device {addr} connected. Total connected: {len(self.connected_devices)}")

        async def on_disconnected(addr):
            async with self._lock:
                self.connected_devices.discard(addr)
            vlog(f"Device {addr} disconnected. Total connected: {len(self.connected_devices)}")

        try:
            while True:
                vlog(f"Attempting to connect to {address} ({name})")

                success = await try_connect_and_stream(dev, ad, asm, on_connected, on_disconnected)
                if success:
                    vlog(f"Connection to {address} completed, will retry")
                    await asyncio.sleep(1.0)  # Small delay before reconnection attempt
                else:
                    vlog(f"Failed to connect to {address}, will retry")
                    await asyncio.sleep(2.0)  # Wait before retry

        except asyncio.CancelledError:
            vlog(f"Connection loop for {address} cancelled")
        except Exception as e:
            vlog(f"Error in connection loop for {address}: {e}")
        finally:
            async with self._lock:
                self.connected_devices.discard(address)

    def get_connected_count(self) -> int:
        """Get number of currently connected devices"""
        return len(self.connected_devices)

    async def shutdown(self):
        """Shutdown all connections"""
        async with self._lock:
            tasks = list(self.active_connections.values())
            self.active_connections.clear()
            self.connected_devices.clear()

        for task in tasks:
            task.cancel()
            with contextlib.suppress(Exception):
                await task

# ========= BLE loop с reconnection =========
async def ble_loop():
    asm = Assembler()
    asyncio.create_task(asm.cleanup_loop())

    device_manager = DeviceManager(max_devices=4)

    try:
        while True:
            cands = await discover_candidates(6.0)
            if not cands:
                await asyncio.sleep(1.0)
                continue

            # приоритет по имени
            cands.sort(key=lambda x: 0 if (DEVICE_NAME_SUBSTR in _name_of(*x)) else 1)

            connected_count = device_manager.get_connected_count()
            vlog(f"Found {len(cands)} candidates, {connected_count}/{device_manager.max_devices} devices connected")

            # Try to connect to new devices
            for dev, ad in cands:
                if device_manager.get_connected_count() >= device_manager.max_devices:
                    break

                address = dev.address
                if address not in device_manager.connected_devices:
                    success = await device_manager.add_device(dev, ad, asm)
                    if success:
                        vlog(f"Started connection attempt for {address}")
                    else:
                        vlog(f"Could not start connection for {address}")

            # Small delay before next discovery
            await asyncio.sleep(2.0)

    except Exception as e:
        vlog(f"BLE loop error: {e}")
    finally:
        await device_manager.shutdown()

# ========= main =========
async def main():
    print(f"WS up: ws://{WS_HOST}:{WS_PORT}/ws/json  |  ws://{WS_HOST}:{WS_PORT}/ws/bin")
    async with serve(ws_router, WS_HOST, WS_PORT, max_size=None):
        await ble_loop()

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, loop.stop)
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()
