//go:build tinygo || wasip1

// The DeepFlow Wasm plugin: teaches deepflow-agent to recognise and decode SMPP.
//
// All the SMPP knowledge lives in ../smpp; this file only translates between
// DeepFlow's plugin interface and that package.
//
// Build:  make plugin      (needs TinyGo; produces smpp.wasm)
package main

import (
	"strconv"

	"github.com/deepflowio/deepflow-wasm-go-sdk/sdk"

	"github.com/MadhuAithal/deepflow-smpp-plugin/smpp"
)

// smppProtocol is the custom protocol number we report to DeepFlow. Custom protocols
// use numbers the agent does not assign to built-in protocols; change it if it clashes.
const smppProtocol uint8 = 200

type parser struct {
	sdk.DefaultParser
}

// HookIn asks the agent to call us for protocol detection and parsing.
func (p parser) HookIn() []sdk.HookBitmap {
	return []sdk.HookBitmap{sdk.HOOK_POINT_PAYLOAD_PARSE}
}

// OnCheckPayload is called on new flows: "is this SMPP?" Returning 0 means no.
func (p parser) OnCheckPayload(ctx *sdk.ParseCtx) (uint8, string, uint8) {
	if ctx.L4 != sdk.TCP {
		return 0, "", 0
	}
	payload, err := ctx.GetPayload()
	if err != nil || !smpp.LooksLikeSMPP(payload) {
		return 0, "", 0
	}
	return smppProtocol, "SMPP", 0
}

// OnParsePayload is called for every captured read on a flow we claimed.
func (p parser) OnParsePayload(ctx *sdk.ParseCtx) sdk.Action {
	if ctx.L4 != sdk.TCP || ctx.L7 != smppProtocol {
		return sdk.ActionNext()
	}
	payload, err := ctx.GetPayload()
	if err != nil {
		return sdk.ActionAbortWithErr(err)
	}

	// A PDU cut off at the end of the read comes back header-only (Truncated=true):
	// enough to pair it by sequence number, never guessed beyond that.
	pdus, _ := smpp.ParseAll(payload)
	var out []*sdk.L7ProtocolInfo
	for i := range pdus {
		pdu := &pdus[i]
		if pdu.IsKeepAlive() {
			continue // enquire_link noise
		}
		out = append(out, toInfo(ctx, pdu))
	}
	if len(out) == 0 {
		return sdk.ActionAbort() // ours, but nothing worth reporting (keep-alives only)
	}
	return sdk.ParseActionAbortWithL7Info(out)
}

func toInfo(ctx *sdk.ParseCtx, pdu *smpp.PDU) *sdk.L7ProtocolInfo {
	// Pairs each request with its response on this connection. This also copes with
	// SMPP windowing (responses out of order), because the agent matches on this ID.
	// Do NOT set IsAsync: in the agent, IsAsync=true means "never pair this record".
	seq := pdu.Sequence

	// DeepFlow sees a connection as client → server. In SMPP the operator also sends
	// requests (deliver_sm with receipts) and the platform answers them, so a request
	// seen on the server → client side, or a response on the client → server side,
	// is "reversed".
	reversed := pdu.IsResponse() == (ctx.Direction == sdk.DirectionRequest)

	info := &sdk.L7ProtocolInfo{
		RequestID: &seq,
		Kv:        toKV(pdu.SafeFields()),
	}
	if reversed {
		info.IsReversed = &reversed
	}

	if pdu.IsResponse() {
		status := respStatus(pdu.Status)
		code := int32(pdu.Status)
		resp := &sdk.Response{
			Status:  &status,
			Code:    &code,
			Result:  pdu.MessageID,
			ReqType: smpp.CommandName(pdu.BaseID()),
		}
		if pdu.Status != 0 {
			resp.Exception = smpp.StatusName(pdu.Status)
		}
		info.Resp = resp
	} else {
		info.Req = &sdk.Request{
			ReqType:  pdu.CommandName(), // submit_sm, deliver_sm, bind_transceiver ...
			Endpoint: pdu.SourceAddr,    // sender ID, e.g. ACMEOTP
			Resource: resource(pdu),     // masked destination, or the bind account
			Domain:   pdu.SystemID,
		}
		if pdu.IsReceipt {
			// a receipt is a request from the operator; its outcome is the receipt status
			info.Req.Resource = "receipt:" + pdu.ReceiptStat
		}
	}
	return info
}

func resource(pdu *smpp.PDU) string {
	if pdu.DestPrefix != "" {
		return pdu.DestPrefix
	}
	return pdu.SystemID
}

func respStatus(status uint32) sdk.RespStatus {
	switch smpp.Classify(status) {
	case smpp.StatusOK:
		return sdk.RespStatusOk
	case smpp.StatusServerError:
		return sdk.RespStatusServerErr
	default:
		return sdk.RespStatusClientErr
	}
}

func toKV(fields [][2]string) []sdk.KeyVal {
	kv := make([]sdk.KeyVal, 0, len(fields))
	for _, f := range fields {
		kv = append(kv, sdk.KeyVal{Key: f[0], Val: f[1]})
	}
	return kv
}

func main() {
	sdk.Info("smpp plugin loaded (protocol " + strconv.Itoa(int(smppProtocol)) + ")")
	sdk.SetParser(parser{})
}
