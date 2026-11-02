#!/usr/bin/env python3
"""
fake_smsc.py - a tiny pretend operator SMSC for learning SMPP.

It speaks just enough SMPP 3.4 to behave like a real operator:
  * accepts bind_transmitter / bind_receiver / bind_transceiver
  * answers enquire_link (keep-alive)
  * answers submit_sm after a random 50-400 ms delay, with a message ID
  * throttles about 1 in 10 submits (command_status 0x58, ESME_RTHROTTLED)
  * 1-6 s later, sends a delivery receipt (deliver_sm) for accepted messages
    that asked for one; about 1 in 8 comes back UNDELIV instead of DELIVRD

Run:  python3 fake_smsc.py            (listens on 127.0.0.1:2775)
      SMSC_PORT=2776 python3 fake_smsc.py   (behind Jasmin, see lab/jasmin)
Stop: Ctrl+C
No dependencies beyond Python 3.8+.
"""
import asyncio
import os
import random
import struct
import time

HOST = "127.0.0.1"
PORT = int(os.environ.get("SMSC_PORT", "2775"))

# command_id values (a response is the request id with the top bit set)
BIND_RECEIVER, BIND_TRANSMITTER, BIND_TRANSCEIVER = 0x01, 0x02, 0x09
SUBMIT_SM, DELIVER_SM, UNBIND, ENQUIRE_LINK = 0x04, 0x05, 0x06, 0x15
RESP = 0x80000000
GENERIC_NACK = 0x80000000

NAMES = {0x01: "bind_receiver", 0x02: "bind_transmitter", 0x09: "bind_transceiver",
         0x04: "submit_sm", 0x05: "deliver_sm", 0x06: "unbind", 0x15: "enquire_link"}

ESME_ROK, ESME_RTHROTTLED = 0x00, 0x58


def name(cmd):
    base = cmd & ~RESP
    n = NAMES.get(base, f"0x{cmd:08x}")
    return n + ("_resp" if cmd & RESP else "")


def pdu(command_id, status, seq, body=b""):
    """Build a PDU: 16-byte header + body. All header fields are 4-byte big-endian."""
    return struct.pack(">IIII", 16 + len(body), command_id, status, seq) + body


def cstr(b, i):
    """Read a NUL-terminated C-octet string starting at i. Returns (value, next_index)."""
    end = b.index(b"\x00", i)
    return b[i:end].decode("latin-1"), end + 1


def parse_submit_sm(body):
    """Pull the fields we care about out of a submit_sm body (SMPP 3.4, section 4.4.1)."""
    i = 0
    service_type, i = cstr(body, i)
    src_ton, src_npi = body[i], body[i + 1]; i += 2
    source_addr, i = cstr(body, i)
    dst_ton, dst_npi = body[i], body[i + 1]; i += 2
    destination_addr, i = cstr(body, i)
    esm_class, protocol_id, priority = body[i], body[i + 1], body[i + 2]; i += 3
    _sched, i = cstr(body, i)
    _validity, i = cstr(body, i)
    registered_delivery = body[i]; i += 1
    _replace, data_coding, _default_msg_id = body[i], body[i + 1], body[i + 2]; i += 3
    sm_length = body[i]; i += 1
    short_message = body[i:i + sm_length]
    return {"source_addr": source_addr, "destination_addr": destination_addr,
            "registered_delivery": registered_delivery, "data_coding": data_coding,
            "esm_class": esm_class, "sm_length": sm_length, "short_message": short_message}


class Session:
    def __init__(self, reader, writer):
        self.r, self.w = reader, writer
        self.peer = writer.get_extra_info("peername")
        self.seq = 0
        self.bound = None

    def next_seq(self):
        self.seq += 1
        return self.seq

    def log(self, direction, cmd, status, seq, extra=""):
        st = "" if status == 0 else f" status=0x{status:02x}"
        print(f"{time.strftime('%H:%M:%S')} {direction} {name(cmd):<22} seq={seq:<5}{st} {extra}", flush=True)

    async def send(self, data):
        self.w.write(data)
        await self.w.drain()

    async def run(self):
        print(f"--- connection from {self.peer}")
        try:
            while True:
                header = await self.r.readexactly(16)
                length, cmd, status, seq = struct.unpack(">IIII", header)
                body = await self.r.readexactly(length - 16) if length > 16 else b""
                await self.handle(cmd, status, seq, body)
        except (asyncio.IncompleteReadError, ConnectionResetError):
            print(f"--- {self.peer} disconnected")

    async def handle(self, cmd, status, seq, body):
        self.log("<-", cmd, status, seq)
        if cmd in (BIND_RECEIVER, BIND_TRANSMITTER, BIND_TRANSCEIVER):
            system_id, _ = cstr(body, 0)
            self.bound = cmd
            await self.send(pdu(cmd | RESP, ESME_ROK, seq, b"FAKESMSC\x00"))
            self.log("->", cmd | RESP, 0, seq, f"(system_id {system_id} logged in)")
        elif cmd == ENQUIRE_LINK:
            await self.send(pdu(ENQUIRE_LINK | RESP, ESME_ROK, seq))
            self.log("->", ENQUIRE_LINK | RESP, 0, seq)
        elif cmd == SUBMIT_SM:
            asyncio.create_task(self.answer_submit(seq, parse_submit_sm(body)))
        elif cmd == (DELIVER_SM | RESP):
            pass  # the client acknowledged our delivery receipt
        elif cmd == UNBIND:
            await self.send(pdu(UNBIND | RESP, ESME_ROK, seq))
            self.log("->", UNBIND | RESP, 0, seq)
        else:
            await self.send(pdu(GENERIC_NACK, 0x03, seq))  # 0x03 = invalid command id

    async def answer_submit(self, seq, sm):
        # Real operators answer out of order under load ("windowing"); random delays reproduce that.
        await asyncio.sleep(random.uniform(0.05, 0.4))
        if random.random() < 0.10:
            await self.send(pdu(SUBMIT_SM | RESP, ESME_RTHROTTLED, seq, b"\x00"))
            self.log("->", SUBMIT_SM | RESP, ESME_RTHROTTLED, seq, f"to={sm['destination_addr']} THROTTLED")
            return
        msg_id = f"{random.getrandbits(32):08X}"
        await self.send(pdu(SUBMIT_SM | RESP, ESME_ROK, seq, msg_id.encode() + b"\x00"))
        self.log("->", SUBMIT_SM | RESP, 0, seq,
                 f"to={sm['destination_addr']} from={sm['source_addr']} len={sm['sm_length']} id={msg_id}")
        if sm["registered_delivery"] & 0x01 and self.bound in (BIND_TRANSCEIVER, BIND_RECEIVER):
            asyncio.create_task(self.send_receipt(sm, msg_id))

    async def send_receipt(self, sm, msg_id):
        await asyncio.sleep(random.uniform(1, 6))
        ok = random.random() > 0.125
        stat, err = ("DELIVRD", "000") if ok else ("UNDELIV", "001")
        now = time.strftime("%y%m%d%H%M")
        text = (f"id:{msg_id} sub:001 dlvrd:{'001' if ok else '000'} submit date:{now} "
                f"done date:{now} stat:{stat} err:{err} text:").encode()
        # deliver_sm: receipt goes FROM the phone number TO the sender ID; esm_class 0x04 marks it as a receipt
        body = (b"\x00" + bytes([1, 1]) + sm["destination_addr"].encode() + b"\x00"
                + bytes([5, 0]) + sm["source_addr"].encode() + b"\x00"
                + bytes([0x04, 0, 0]) + b"\x00" + b"\x00" + bytes([0, 0, 0, 0])
                + bytes([len(text)]) + text)
        seq = self.next_seq()
        await self.send(pdu(DELIVER_SM, ESME_ROK, seq, body))
        self.log("->", DELIVER_SM, 0, seq, f"receipt id={msg_id} stat={stat}")


async def main():
    server = await asyncio.start_server(lambda r, w: Session(r, w).run(), HOST, PORT)
    print(f"fake SMSC listening on {HOST}:{PORT}  (Ctrl+C to stop)")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nstopped")
