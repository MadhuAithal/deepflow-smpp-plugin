#!/usr/bin/env python3
"""
send_otps.py - plays the platform side (the ESME): binds to the SMSC, sends OTPs,
waits for delivery receipts, then unbinds.

Setup:  python3 -m venv ~/smpp-venv && ~/smpp-venv/bin/pip install smpplib
Run:    ~/smpp-venv/bin/python send_otps.py            (10 OTPs to 127.0.0.1:2775)
        WAIT=150 ~/smpp-venv/bin/python send_otps.py 30   (listen 150 s for late receipts)
        ~/smpp-venv/bin/python send_otps.py 25         (25 OTPs)
"""
import os
import random
import select
import sys
import time

import smpplib.client
import smpplib.consts

COUNT = int(sys.argv[1]) if len(sys.argv) > 1 else 10
WAIT_FOR_RECEIPTS = int(os.environ.get("WAIT", "8"))  # seconds to keep listening after the last submit

client = smpplib.client.Client("127.0.0.1", 2775, allow_unknown_opt_params=True)
sent = {}


def on_sent(pdu):
    # submit_sm_resp: same sequence number as our submit_sm; carries the operator's message ID
    status = "OK" if pdu.status == 0 else f"ERROR 0x{pdu.status:02x}"
    mid = pdu.message_id.decode() if pdu.message_id else "-"
    print(f"  submit_sm_resp seq={pdu.sequence:<4} {status:<10} message_id={mid}")


def on_received(pdu):
    # deliver_sm: here, a delivery receipt (esm_class 0x04); the link back is the message ID in the text
    print(f"  deliver_sm     seq={pdu.sequence:<4} receipt: {pdu.short_message.decode(errors='replace')[:60]}")


client.set_message_sent_handler(on_sent)
client.set_message_received_handler(on_received)
client.connect()
client.bind_transceiver(system_id="acme", password="secret")
print(f"bound; sending {COUNT} OTPs")

for _ in range(COUNT):
    phone = "91" + random.choice("6789") + "".join(random.choices("0123456789", k=9))
    otp = f"{random.randint(0, 999999):06d}"
    pdu = client.send_message(
        source_addr_ton=smpplib.consts.SMPP_TON_ALNUM,
        source_addr_npi=smpplib.consts.SMPP_NPI_UNK,
        source_addr="ACMEOTP",
        dest_addr_ton=smpplib.consts.SMPP_TON_INTL,
        dest_addr_npi=smpplib.consts.SMPP_NPI_ISDN,
        destination_addr=phone,
        short_message=f"Your OTP is {otp}. Do not share it.".encode(),
        registered_delivery=True,
    )
    print(f"submit_sm        seq={pdu.sequence:<4} to={phone}")

# keep reading responses and receipts for a while. Wait until data is waiting, then read a
# whole PDU: a timeout in the middle of a PDU would lose our place in the stream.
deadline = time.time() + WAIT_FOR_RECEIPTS
client._socket.settimeout(10)
while time.time() < deadline:
    ready, _, _ = select.select([client._socket], [], [], 0.5)
    if ready:
        client.read_once()

client.unbind()
client.disconnect()
print("done")
