// smpp-decode prints what the parser sees in a hex dump of SMPP bytes.
//
// In Wireshark: right-click "Short Message Peer to Peer" → Copy → "...as a Hex Stream", then
//
//	echo <paste> | go run ./cmd/smpp-decode
//
// Spaces, newlines and 0x prefixes in the input are ignored.
package main

import (
	"bufio"
	"encoding/hex"
	"fmt"
	"os"
	"strings"

	"github.com/MadhuAithal/deepflow-smpp-plugin/smpp"
)

func main() {
	var sb strings.Builder
	sc := bufio.NewScanner(os.Stdin)
	sc.Buffer(make([]byte, 1<<20), 1<<20)
	for sc.Scan() {
		sb.WriteString(sc.Text())
	}
	clean := strings.NewReplacer(" ", "", "\t", "", "0x", "", ":", "").Replace(sb.String())
	b, err := hex.DecodeString(clean)
	if err != nil {
		fmt.Fprintln(os.Stderr, "not valid hex:", err)
		os.Exit(1)
	}
	fmt.Printf("%d bytes, looks like SMPP: %v\n", len(b), smpp.LooksLikeSMPP(b))
	pdus, truncated := smpp.ParseAll(b)
	for i, p := range pdus {
		fmt.Printf("\nPDU %d\n", i+1)
		for _, kv := range p.SafeFields() {
			fmt.Printf("  %-26s %s\n", kv[0], kv[1])
		}
	}
	if truncated {
		fmt.Println("\n(last PDU was cut off and skipped)")
	}
}
