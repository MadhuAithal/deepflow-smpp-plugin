//go:build tinygo

package main

// DeepFlow recommends nottinygc as TinyGo's memory allocator for plugins:
// the default one can be slow under heavy traffic. Needs -gc=custom -tags=custommalloc.
import _ "github.com/wasilibs/nottinygc"
