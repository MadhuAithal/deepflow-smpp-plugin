# SMPP plugin for DeepFlow
#
#   make test      run the parser tests (plain Go, no DeepFlow needed)
#   make fuzz      throw random bytes at the parser for 30 s
#   make plugin    build smpp.wasm with TinyGo (see README: Go 1.21 + TinyGo 0.30)
#   make upload    upload smpp.wasm to the DeepFlow server
#   make config    apply deploy/agent-group-config.yaml to GROUP_ID
#   make check     show recent SMPP rows from DeepFlow
#
# Settings (override on the command line):
#   make upload NODE_IP=192.168.0.24
#   make config GROUP_ID=g-xxxxxxxxxx

NODE_IP  ?= 10.233.233.1
GROUP_ID ?=
NAME     ?= smpp
MAX_WASM ?= 1040000   # bytes; the agent fetches plugins with a 1 MiB message limit

.PHONY: deps test fuzz plugin upload config check clean

deps:
	go mod tidy

test:
	go test ./smpp -v

fuzz:
	go test ./smpp -run xxx -fuzz FuzzParseAll -fuzztime 30s

# DeepFlow's documented flags: nottinygc allocator, WASI target, no goroutine scheduler.
plugin:
	tinygo build -o $(NAME).wasm -gc=custom -tags=custommalloc -target=wasi \
		-panic=trap -scheduler=none -no-debug ./plugin
	@size=$$(stat -c %s $(NAME).wasm); echo "$(NAME).wasm: $$size bytes"; \
	if [ $$size -gt $(MAX_WASM) ]; then \
		echo "Too large: the agent cannot fetch plugins of 1 MiB or more. See FINDINGS.md #4."; exit 1; fi

# Re-uploading the same name fails, so delete any previous copy first.
upload: plugin
	-deepflow-ctl -i $(NODE_IP) plugin delete $(NAME) 2>/dev/null
	deepflow-ctl -i $(NODE_IP) plugin create --type wasm --image $(NAME).wasm --name $(NAME)
	deepflow-ctl -i $(NODE_IP) plugin list

config:
	@[ -n "$(GROUP_ID)" ] || { echo "Set GROUP_ID, e.g. make config GROUP_ID=g-xxxxxxxxxx (deepflow-ctl agent-group list)"; exit 1; }
	deepflow-ctl -i $(NODE_IP) agent-group-config update $(GROUP_ID) -f deploy/agent-group-config.yaml

check:
	@curl -sS -XPOST "http://$(NODE_IP):20416/v1/query/" \
		--data-urlencode "db=flow_log" \
		--data-urlencode "sql=SELECT request_type, type, tap_side, endpoint, request_resource, response_code, response_exception, response_result, response_duration FROM l7_flow_log WHERE server_port=2775 OR client_port=2775 ORDER BY time DESC LIMIT 20" \
		| python3 -m json.tool

clean:
	rm -f $(NAME).wasm
