#!/usr/bin/env bash
# firewall-netns-harness.sh — load scripts/firewall/lifekit-firewall.nft into a
# throwaway network namespace and probe it with real traffic. Run it only as
# `unshare -rnpf --mount-proc bash firewall-netns-harness.sh <ruleset>`: the
# user namespace gives CAP_NET_ADMIN over a netns nobody else uses, so nothing
# on the real host is touched, and the pid namespace takes every server and
# peer down with the harness. test_host_firewall.py drives it.
#
# The namespace it runs in plays the host. Three peer namespaces hang off it
# on veth pairs whose host ends carry the interface names the ruleset keys on:
#
#   client     tailscale0 10.1.0.1 <-> 10.1.0.2   (a tailnet peer)
#              pub0       10.2.0.1 <-> 10.2.0.2   (the internet)
#   container  br-test0   172.30.0.1 <-> 172.30.0.2   (a compose network)
#   edge       lifekit-edge 172.31.0.1 <-> 172.31.0.2 (the edge network)
#
# A docker-like nat table stands in for published ports: host :3000 -> the
# container's :8080 (a tailnet-bound publish), host :443 -> the edge's :8443
# and host :9443 -> the edge's :443. A docker-like filter table carries a
# DOCKER-USER chain, so the run also shows the ruleset leaves both alone.
#
# Prints one `<probe>=ok|blocked` line per probe, then `foreign_tables=intact`
# when the docker-like tables survived two loads of the ruleset unchanged.

set -euo pipefail

RULESET="$1"
export PATH="${PATH}:/usr/sbin:/sbin"

PIDS=()
cleanup() { kill "${PIDS[@]}" 2>/dev/null || true; }
trap cleanup EXIT

peer() { # var: start a namespace and store its pid in var
  unshare -n sleep 120 >/dev/null &
  PIDS+=("$!")
  printf -v "$1" '%s' "$!"
}

client='' container='' edge=''
peer client
peer container
peer edge
sleep 0.2
run_in() { local pid="$1"; shift; nsenter -t "${pid}" -n "$@"; }

link() { # host-if host-addr peer-pid peer-if peer-addr
  ip link add "$1" type veth peer name "$4" netns "$3"
  ip addr add "$2/24" dev "$1"
  ip link set "$1" up
  run_in "$3" ip addr add "$5/24" dev "$4"
  run_in "$3" ip link set "$4" up
  run_in "$3" ip link set lo up
}

ip link set lo up
link tailscale0 10.1.0.1 "${client}" c-ts 10.1.0.2
link pub0 10.2.0.1 "${client}" c-pub 10.2.0.2
link br-test0 172.30.0.1 "${container}" ct0 172.30.0.2
link lifekit-edge 172.31.0.1 "${edge}" e0 172.31.0.2
sysctl -qw net.ipv4.ip_forward=1
run_in "${container}" ip route add default via 172.30.0.1
run_in "${edge}" ip route add default via 172.31.0.1
# The client reaches the container networks over its public link.
run_in "${client}" ip route add 172.30.0.0/24 via 10.2.0.1
run_in "${client}" ip route add 172.31.0.0/24 via 10.2.0.1

nft -f - <<'EOF'
table ip filter {
	chain DOCKER-USER {
		counter return
	}
	chain FORWARD {
		type filter hook forward priority filter; policy accept;
		jump DOCKER-USER
	}
}
table ip nat {
	chain PREROUTING {
		type nat hook prerouting priority dstnat; policy accept;
		tcp dport 3000 dnat to 172.30.0.2:8080
		tcp dport 443 dnat to 172.31.0.2:8443
		tcp dport 9443 dnat to 172.31.0.2:443
	}
}
EOF
foreign_before="$(nft -s list table ip filter; nft -s list table ip nat)"

nft -f "${RULESET}"
nft -f "${RULESET}"

serve() { # pid proto port
  nsenter -t "$1" -n python3 - "$2" "$3" >/dev/null 2>&1 <<'PY' &
import socket, sys
proto, port = sys.argv[1], int(sys.argv[2])
if proto == "tcp":
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", port))
    s.listen()
    while True:
        c, _ = s.accept()
        c.sendall(b"ok")
        c.close()
else:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("0.0.0.0", port))
    while True:
        data, addr = s.recvfrom(64)
        s.sendto(b"ok", addr)
PY
  PIDS+=("$!")
}

probe() { # name pid proto addr port
  local result
  result="$(run_in "$2" python3 - "$3" "$4" "$5" <<'PY'
import socket, sys
proto, addr, port = sys.argv[1], sys.argv[2], int(sys.argv[3])
kind = socket.SOCK_STREAM if proto == "tcp" else socket.SOCK_DGRAM
s = socket.socket(socket.AF_INET, kind)
s.settimeout(1.5)
try:
    if proto == "tcp":
        s.connect((addr, port))
    else:
        s.sendto(b"hi", (addr, port))
    print("ok" if s.recv(64) == b"ok" else "blocked")
except OSError:
    print("blocked")
PY
)"
  echo "$1=${result}"
}

host=$$
for port in 22 80 8080; do serve "${host}" tcp "${port}"; done
serve "${host}" udp 41641
serve "${host}" udp 5353
serve "${container}" tcp 8080
serve "${edge}" tcp 443
serve "${edge}" tcp 8443
serve "${client}" tcp 9000
sleep 1

probe tailnet_ssh "${client}" tcp 10.1.0.1 22
probe public_ssh "${client}" tcp 10.2.0.1 22
probe tailnet_other_port "${client}" tcp 10.1.0.1 8080
probe public_http "${client}" tcp 10.2.0.1 80
probe public_other_port "${client}" tcp 10.2.0.1 8080
probe public_wireguard "${client}" udp 10.2.0.1 41641
probe public_other_udp "${client}" udp 10.2.0.1 5353
probe tailnet_published_port "${client}" tcp 10.1.0.1 3000
probe public_published_port "${client}" tcp 10.2.0.1 3000
probe public_routed_to_container "${client}" tcp 172.30.0.2 8080
probe public_edge_https "${client}" tcp 10.2.0.1 443
probe public_edge_other_port "${client}" tcp 10.2.0.1 9443
probe container_egress "${container}" tcp 10.2.0.2 9000
probe container_to_host "${container}" tcp 172.30.0.1 8080
probe edge_to_container "${edge}" tcp 172.30.0.2 8080
probe host_loopback "${host}" tcp 127.0.0.1 8080

if [[ "$(nft -s list table ip filter; nft -s list table ip nat)" == "${foreign_before}" ]]; then
  echo "foreign_tables=intact"
else
  echo "foreign_tables=changed"
fi
