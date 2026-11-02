// Package smpp decodes SMPP 3.4 PDUs from raw bytes captured off the wire.
//
// It is deliberately independent of DeepFlow, so it can be tested with plain
// `go test`. The DeepFlow plugin in ../plugin wraps it.
//
// Design rules, because this runs inside an observability agent:
//   - never panic, whatever bytes arrive
//   - be strict about what counts as SMPP (false positives pollute other protocols)
//   - never expose message text, passwords or full phone numbers
package smpp

import (
	"bytes"
	"encoding/binary"
	"errors"
	"fmt"
	"strings"
)

// HeaderLen is the fixed size of every SMPP header:
// command_length, command_id, command_status, sequence_number (4 bytes each, big-endian).
const HeaderLen = 16

// MaxPDULen caps command_length. Real PDUs are far smaller; anything bigger is not SMPP.
const MaxPDULen = 64 * 1024

// RespBit is set in command_id for every response PDU.
const RespBit uint32 = 0x80000000

// Command IDs (SMPP 3.4, section 5.1.2.1).
const (
	GenericNack       uint32 = 0x80000000
	BindReceiver      uint32 = 0x00000001
	BindTransmitter   uint32 = 0x00000002
	QuerySM           uint32 = 0x00000003
	SubmitSM          uint32 = 0x00000004
	DeliverSM         uint32 = 0x00000005
	Unbind            uint32 = 0x00000006
	ReplaceSM         uint32 = 0x00000007
	CancelSM          uint32 = 0x00000008
	BindTransceiver   uint32 = 0x00000009
	Outbind           uint32 = 0x0000000B
	EnquireLink       uint32 = 0x00000015
	SubmitMulti       uint32 = 0x00000021
	AlertNotification uint32 = 0x00000102
	DataSM            uint32 = 0x00000103
)

var commandNames = map[uint32]string{
	BindReceiver: "bind_receiver", BindTransmitter: "bind_transmitter", QuerySM: "query_sm",
	SubmitSM: "submit_sm", DeliverSM: "deliver_sm", Unbind: "unbind", ReplaceSM: "replace_sm",
	CancelSM: "cancel_sm", BindTransceiver: "bind_transceiver", Outbind: "outbind",
	EnquireLink: "enquire_link", SubmitMulti: "submit_multi", AlertNotification: "alert_notification",
	DataSM: "data_sm",
}

// requests that have no response PDU in the protocol
var noResponse = map[uint32]bool{Outbind: true, AlertNotification: true}

// Status codes worth naming (SMPP 3.4, section 5.1.3).
var statusNames = map[uint32]string{
	0x00: "ESME_ROK", 0x01: "ESME_RINVMSGLEN", 0x02: "ESME_RINVCMDLEN", 0x03: "ESME_RINVCMDID",
	0x04: "ESME_RINVBNDSTS", 0x05: "ESME_RALYBND", 0x06: "ESME_RINVPRTFLG", 0x07: "ESME_RINVREGDLVFLG",
	0x08: "ESME_RSYSERR", 0x0A: "ESME_RINVSRCADR", 0x0B: "ESME_RINVDSTADR", 0x0C: "ESME_RINVMSGID",
	0x0D: "ESME_RBINDFAIL", 0x0E: "ESME_RINVPASWD", 0x0F: "ESME_RINVSYSID", 0x11: "ESME_RCANCELFAIL",
	0x13: "ESME_RREPLACEFAIL", 0x14: "ESME_RMSGQFUL", 0x15: "ESME_RINVSERTYP", 0x33: "ESME_RINVNUMDESTS",
	0x34: "ESME_RINVDLNAME", 0x40: "ESME_RINVDESTFLAG", 0x42: "ESME_RINVSUBREP", 0x43: "ESME_RINVESMCLASS",
	0x44: "ESME_RCNTSUBDL", 0x45: "ESME_RSUBMITFAIL", 0x48: "ESME_RINVSRCTON", 0x49: "ESME_RINVSRCNPI",
	0x50: "ESME_RINVDSTTON", 0x51: "ESME_RINVDSTNPI", 0x53: "ESME_RINVSYSTYP", 0x54: "ESME_RINVREPFLAG",
	0x55: "ESME_RINVNUMMSGS", 0x58: "ESME_RTHROTTLED", 0x61: "ESME_RINVSCHED", 0x62: "ESME_RINVEXPIRY",
	0x63: "ESME_RINVDFTMSGID", 0x64: "ESME_RX_T_APPN", 0x65: "ESME_RX_P_APPN", 0x66: "ESME_RX_R_APPN",
	0x67: "ESME_RQUERYFAIL", 0xC0: "ESME_RINVOPTPARSTREAM", 0xC1: "ESME_ROPTPARNOTALLWD",
	0xC2: "ESME_RINVPARLEN", 0xC3: "ESME_RMISSINGOPTPARAM", 0xC4: "ESME_RINVOPTPARAMVAL",
	0xFE: "ESME_RDELIVERYFAILURE", 0xFF: "ESME_RUNKNOWNERR",
}

// StatusClass groups command_status into the buckets an observability tool cares about.
type StatusClass int

const (
	StatusOK          StatusClass = iota
	StatusClientError             // the sender did something wrong (bad address, bad parameters, bad login)
	StatusServerError             // the operator side refused or failed (throttled, queue full, system error)
)

// serverSide lists statuses that point at the operator rather than the sender.
var serverSide = map[uint32]bool{
	0x08: true, 0x14: true, 0x45: true, 0x58: true, 0x64: true, 0x65: true, 0x66: true,
	0x67: true, 0xFE: true, 0xFF: true,
}

// Classify maps a command_status to OK / client error / server error.
func Classify(status uint32) StatusClass {
	switch {
	case status == 0:
		return StatusOK
	case serverSide[status]:
		return StatusServerError
	default:
		return StatusClientError
	}
}

// StatusName returns the SMPP name for a status code, or its hex value if unknown.
func StatusName(status uint32) string {
	if n, ok := statusNames[status]; ok {
		return n
	}
	return fmt.Sprintf("0x%08X", status)
}

// PDU is one decoded SMPP message. Only safe-to-export fields are kept.
type PDU struct {
	Length   uint32
	ID       uint32 // command_id, including RespBit for responses
	Status   uint32
	Sequence uint32

	// submit_sm / deliver_sm / data_sm
	SourceAddr         string // sender ID, e.g. ACMEOTP (a brand, not a person)
	DestPrefix         string // destination with the subscriber part masked
	ESMClass           uint8
	RegisteredDelivery uint8
	DataCoding         uint8
	SMLength           int // length of the message text; the text itself is never kept
	ConcatRef          int // long-message reference, 0 if not concatenated
	ConcatTotal        int
	ConcatPart         int

	// responses and receipts
	MessageID   string // operator's ID from submit_sm_resp, or the id: in a delivery receipt
	IsReceipt   bool   // deliver_sm carrying a delivery receipt (esm_class bit 0x04)
	ReceiptStat string // DELIVRD, UNDELIV, EXPIRED, REJECTD ...
	ReceiptErr  string

	// bind_*
	SystemID string // the account name; the password is never kept

	// Truncated is set when only the header was captured (the read ended mid-PDU).
	Truncated bool
}

// IsResponse reports whether the PDU is a response (including generic_nack).
func (p *PDU) IsResponse() bool { return p.ID&RespBit != 0 }

// BaseID is the command_id without the response bit.
func (p *PDU) BaseID() uint32 { return p.ID &^ RespBit }

// IsKeepAlive reports enquire_link and its response, which carry no business meaning.
func (p *PDU) IsKeepAlive() bool { return p.BaseID() == EnquireLink }

// CommandName returns e.g. "submit_sm" or "submit_sm_resp".
func (p *PDU) CommandName() string { return CommandName(p.ID) }

// CommandName names any command_id.
func CommandName(id uint32) string {
	if id == GenericNack {
		return "generic_nack"
	}
	n, ok := commandNames[id&^RespBit]
	if !ok {
		return fmt.Sprintf("0x%08X", id)
	}
	if id&RespBit != 0 {
		return n + "_resp"
	}
	return n
}

// KnownCommand reports whether id is a command defined by SMPP 3.4.
func KnownCommand(id uint32) bool {
	if id == GenericNack {
		return true
	}
	base := id &^ RespBit
	if _, ok := commandNames[base]; !ok {
		return false
	}
	if id&RespBit != 0 && noResponse[base] {
		return false
	}
	return true
}

// ErrNotSMPP means the bytes do not look like an SMPP header.
var ErrNotSMPP = errors.New("not an SMPP PDU")

// ErrTruncated means a header was valid but the PDU continues beyond the captured bytes.
var ErrTruncated = errors.New("truncated SMPP PDU")

type header struct{ length, id, status, seq uint32 }

func readHeader(b []byte) (header, error) {
	if len(b) < HeaderLen {
		return header{}, ErrTruncated
	}
	h := header{
		length: binary.BigEndian.Uint32(b[0:4]),
		id:     binary.BigEndian.Uint32(b[4:8]),
		status: binary.BigEndian.Uint32(b[8:12]),
		seq:    binary.BigEndian.Uint32(b[12:16]),
	}
	if h.length < HeaderLen || h.length > MaxPDULen || !KnownCommand(h.id) {
		return h, ErrNotSMPP
	}
	// requests always carry status 0 (generic_nack is a response, so it is allowed a status)
	if h.id&RespBit == 0 && h.status != 0 {
		return h, ErrNotSMPP
	}
	// sequence_number range is 0x00000001 to 0x7FFFFFFF
	if h.seq == 0 || h.seq > 0x7FFFFFFF {
		return h, ErrNotSMPP
	}
	// commands that never carry a body must be exactly 16 bytes
	switch h.id {
	case EnquireLink, EnquireLink | RespBit, Unbind, Unbind | RespBit, GenericNack:
		if h.length != HeaderLen {
			return h, ErrNotSMPP
		}
	}
	return h, nil
}

// LooksLikeSMPP is the strict detector used to claim a TCP flow.
// It requires the first PDU to be complete and to decode cleanly.
func LooksLikeSMPP(b []byte) bool {
	h, err := readHeader(b)
	if err != nil || int(h.length) > len(b) {
		return false
	}
	_, err = Decode(b[:h.length])
	return err == nil
}

// ParseAll splits a captured buffer into PDUs. One read often holds several.
// If the last PDU is cut off but its 16-byte header is complete, it is returned
// header-only with Truncated set: command, status and sequence are known, the body
// is not guessed at. Anything that stops looking like SMPP ends the walk.
func ParseAll(b []byte) (pdus []PDU, truncated bool) {
	for len(b) > 0 {
		h, err := readHeader(b)
		if err == ErrTruncated {
			return pdus, true
		}
		if err != nil {
			return pdus, false
		}
		if int(h.length) > len(b) {
			pdus = append(pdus, PDU{Length: h.length, ID: h.id, Status: h.status, Sequence: h.seq, Truncated: true})
			return pdus, true
		}
		p, err := Decode(b[:h.length])
		if err != nil {
			return pdus, false
		}
		pdus = append(pdus, p)
		b = b[h.length:]
	}
	return pdus, false
}

// Decode decodes exactly one complete PDU.
func Decode(b []byte) (PDU, error) {
	h, err := readHeader(b)
	if err != nil {
		return PDU{}, err
	}
	if int(h.length) != len(b) {
		return PDU{}, ErrTruncated
	}
	p := PDU{Length: h.length, ID: h.id, Status: h.status, Sequence: h.seq}
	r := &reader{b: b[HeaderLen:]}

	switch h.id {
	case BindReceiver, BindTransmitter, BindTransceiver:
		p.SystemID = r.cstr(16)
		r.cstr(9) // password: read past it, never keep it
	case BindReceiver | RespBit, BindTransmitter | RespBit, BindTransceiver | RespBit:
		if r.left() > 0 {
			p.SystemID = r.cstr(16)
		}
	case SubmitSM, DeliverSM:
		decodeShortMessage(r, &p)
	case DataSM:
		r.cstr(6) // service_type
		r.u8()
		r.u8()
		p.SourceAddr = r.cstr(65)
		r.u8()
		r.u8()
		p.DestPrefix = MaskNumber(r.cstr(65))
		p.ESMClass = r.u8()
		p.RegisteredDelivery = r.u8()
		p.DataCoding = r.u8()
	case SubmitSM | RespBit, DeliverSM | RespBit, DataSM | RespBit:
		// on error the body may be empty or a lone NUL
		if r.left() > 0 {
			p.MessageID = r.cstr(65)
		}
	}
	if r.err != nil {
		return PDU{}, r.err
	}
	return p, nil
}

// decodeShortMessage reads the shared submit_sm / deliver_sm body layout (SMPP 3.4, 4.4.1 and 4.6.1).
func decodeShortMessage(r *reader, p *PDU) {
	r.cstr(6) // service_type
	r.u8()    // source_addr_ton
	r.u8()    // source_addr_npi
	src := r.cstr(21)
	r.u8() // dest_addr_ton
	r.u8() // dest_addr_npi
	dst := r.cstr(21)
	p.ESMClass = r.u8()
	r.u8()     // protocol_id
	r.u8()     // priority_flag
	r.cstr(17) // schedule_delivery_time
	r.cstr(17) // validity_period
	p.RegisteredDelivery = r.u8()
	r.u8() // replace_if_present_flag
	p.DataCoding = r.u8()
	r.u8() // sm_default_msg_id
	smLen := int(r.u8())
	sm := r.bytes(smLen)
	if r.err != nil {
		return
	}
	p.SMLength = smLen

	if p.ID == DeliverSM && p.ESMClass&0x04 != 0 {
		// Delivery receipt: the operator sends it FROM the handset TO the sender ID,
		// so swap the roles back to keep fields meaning the same thing on every PDU.
		p.IsReceipt = true
		p.SourceAddr = dst
		p.DestPrefix = MaskNumber(src)
		parseReceiptText(string(sm), p)
	} else {
		p.SourceAddr = src
		p.DestPrefix = MaskNumber(dst)
		if p.ESMClass&0x40 != 0 { // UDHI: the message starts with a user data header
			parseConcat(sm, p)
		}
	}
	// Optional TLVs follow; receipts may carry the ID and state there instead of in the text.
	parseTLVs(r.rest(), p)
}

// parseReceiptText reads the de-facto receipt format:
// "id:IIIIIIIIII sub:SSS dlvrd:DDD submit date:YYMMDDhhmm done date:YYMMDDhhmm stat:DDDDDDD err:E text: ..."
// Only id, stat and err are kept; the trailing text: may echo the message, so it is ignored.
func parseReceiptText(s string, p *PDU) {
	if i := strings.Index(s, " text:"); i >= 0 {
		s = s[:i]
	}
	for _, f := range strings.Fields(s) {
		switch {
		case strings.HasPrefix(f, "id:"):
			p.MessageID = f[3:]
		case strings.HasPrefix(f, "stat:"):
			p.ReceiptStat = f[5:]
		case strings.HasPrefix(f, "err:"):
			p.ReceiptErr = f[4:]
		}
	}
}

// parseConcat reads the concatenation information element from a user data header.
func parseConcat(sm []byte, p *PDU) {
	if len(sm) < 1 {
		return
	}
	udhl := int(sm[0])
	if udhl+1 > len(sm) {
		return
	}
	udh := sm[1 : 1+udhl]
	for len(udh) >= 2 {
		iei, iel := udh[0], int(udh[1])
		if 2+iel > len(udh) {
			return
		}
		v := udh[2 : 2+iel]
		switch {
		case iei == 0x00 && iel == 3: // 8-bit reference
			p.ConcatRef, p.ConcatTotal, p.ConcatPart = int(v[0]), int(v[1]), int(v[2])
		case iei == 0x08 && iel == 4: // 16-bit reference
			p.ConcatRef, p.ConcatTotal, p.ConcatPart = int(binary.BigEndian.Uint16(v[0:2])), int(v[2]), int(v[3])
		}
		udh = udh[2+iel:]
	}
}

// TLV tags used here (SMPP 3.4, section 5.3.2).
const (
	tagReceiptedMessageID = 0x001E
	tagMessageState       = 0x0427
	tagMessagePayload     = 0x0424
)

var messageStates = map[byte]string{
	1: "ENROUTE", 2: "DELIVRD", 3: "EXPIRED", 4: "DELETED", 5: "UNDELIV", 6: "ACCEPTD", 7: "UNKNOWN", 8: "REJECTD",
}

func parseTLVs(b []byte, p *PDU) {
	for len(b) >= 4 {
		tag := binary.BigEndian.Uint16(b[0:2])
		n := int(binary.BigEndian.Uint16(b[2:4]))
		if 4+n > len(b) {
			return
		}
		v := b[4 : 4+n]
		switch tag {
		case tagReceiptedMessageID:
			if p.MessageID == "" {
				p.MessageID = string(bytes.TrimRight(v, "\x00"))
			}
		case tagMessageState:
			if len(v) == 1 && p.ReceiptStat == "" {
				p.ReceiptStat = messageStates[v[0]]
			}
		case tagMessagePayload:
			if p.SMLength == 0 { // long text sent as a TLV instead of short_message
				p.SMLength = n
			}
		}
		b = b[4+n:]
	}
}

// KeepDigits is how many leading digits of a phone number are kept.
// For Indian numbers, 6 keeps the country code and the 4-digit series (enough to infer the
// operator and circle) and masks the 6 digits that identify the subscriber.
var KeepDigits = 6

// MaskNumber keeps the leading KeepDigits digits of a numeric address and masks the rest.
// Alphanumeric sender IDs are brands, not people, and are returned unchanged.
func MaskNumber(s string) string {
	t := strings.TrimPrefix(s, "+")
	for _, c := range t {
		if c < '0' || c > '9' {
			return s
		}
	}
	if len(t) <= KeepDigits {
		return strings.Repeat("x", len(t))
	}
	return t[:KeepDigits] + strings.Repeat("x", len(t)-KeepDigits)
}

// SafeFields returns the fields that may leave the machine, as key/value pairs.
// Message text, passwords and full phone numbers are never included.
func (p *PDU) SafeFields() [][2]string {
	kv := [][2]string{{"smpp.command", p.CommandName()}, {"smpp.sequence", fmt.Sprint(p.Sequence)}}
	add := func(k, v string) {
		if v != "" {
			kv = append(kv, [2]string{k, v})
		}
	}
	if p.IsResponse() {
		add("smpp.status", StatusName(p.Status))
	}
	if p.Truncated {
		add("smpp.truncated", "true")
		return kv // body fields are unknown
	}
	add("smpp.system_id", p.SystemID)
	add("smpp.source_addr", p.SourceAddr)
	add("smpp.dest_prefix", p.DestPrefix)
	add("smpp.message_id", p.MessageID)
	switch p.BaseID() {
	case SubmitSM, DeliverSM, DataSM:
		if !p.IsResponse() {
			add("smpp.data_coding", fmt.Sprint(p.DataCoding))
			add("smpp.registered_delivery", fmt.Sprint(p.RegisteredDelivery))
			add("smpp.sm_length", fmt.Sprint(p.SMLength))
		}
	}
	if p.IsReceipt {
		add("smpp.receipt", "true")
		add("smpp.receipt_stat", p.ReceiptStat)
		add("smpp.receipt_err", p.ReceiptErr)
	}
	if p.ConcatTotal > 0 {
		add("smpp.concat_ref", fmt.Sprint(p.ConcatRef))
		add("smpp.concat_part", fmt.Sprintf("%d/%d", p.ConcatPart, p.ConcatTotal))
	}
	return kv
}

// reader walks a body without ever indexing out of range; the first problem sticks in err.
type reader struct {
	b   []byte
	off int
	err error
}

var errShort = errors.New("SMPP body shorter than its fields")
var errCString = errors.New("SMPP C-octet string missing terminator or too long")

func (r *reader) left() int { return len(r.b) - r.off }

func (r *reader) u8() uint8 {
	if r.err != nil || r.left() < 1 {
		r.err = firstErr(r.err, errShort)
		return 0
	}
	v := r.b[r.off]
	r.off++
	return v
}

func (r *reader) bytes(n int) []byte {
	if r.err != nil || r.left() < n {
		r.err = firstErr(r.err, errShort)
		return nil
	}
	v := r.b[r.off : r.off+n]
	r.off += n
	return v
}

// cstr reads a NUL-terminated string of at most max bytes including the NUL.
func (r *reader) cstr(max int) string {
	if r.err != nil {
		return ""
	}
	end := bytes.IndexByte(r.b[r.off:], 0)
	if end < 0 || end+1 > max {
		r.err = errCString
		return ""
	}
	v := string(r.b[r.off : r.off+end])
	r.off += end + 1
	return v
}

func (r *reader) rest() []byte {
	if r.err != nil {
		return nil
	}
	return r.b[r.off:]
}

func firstErr(a, b error) error {
	if a != nil {
		return a
	}
	return b
}
