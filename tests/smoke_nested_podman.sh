#!/bin/sh
# Run inside a workspace with the podman group; requires registry access.
set -eu
test -f /run/.containerenv
test "$(podman info --format '{{.Host.Security.Rootless}}')" = true
test "$(podman info --format '{{.Store.GraphDriverName}}')" = overlay

work=$(mktemp -d)
name="ab-smoke-$(basename "$work" | tr '[:upper:]' '[:lower:]')"
image="localhost/$name"
volume="$name-data"
cleanup() {
    podman rm -f --time 1 "$name" >/dev/null 2>&1 || true
    podman volume rm "$volume" >/dev/null 2>&1 || true
    podman rmi "$image" >/dev/null 2>&1 || true
    rm -rf "$work"
}
trap cleanup EXIT HUP INT TERM
mkdir "$work/context" "$work/bind"
cat > "$work/context/Containerfile" <<'EOF'
FROM docker.io/library/alpine:3.23
RUN apk add --no-cache busybox-extras && touch /owner && chown 65534:65534 /owner
CMD ["sh"]
EOF
podman build -t "$image" "$work/context"
podman volume create "$volume"
podman run -d --name "$name" -p 127.0.0.1::8080 \
    -v "$work/bind:/bind" -v "$volume:/data" "$image" sh -ec '
    test "$(stat -c %u:%g /owner)" = 65534:65534
    echo bind-ok > /bind/result
    echo volume-ok > /data/result
    mkdir /www; echo http-ok > /www/index.html
    exec httpd -f -p 8080 -h /www'
address=$(podman port "$name" 8080/tcp)
test "$(curl -fsS --retry 10 --retry-connrefused --retry-delay 1 "http://$address")" = http-ok
podman exec "$name" test -f /owner
test "$(cat "$work/bind/result")" = bind-ok
podman rm -f --time 1 "$name"
test "$(podman run --rm -v "$volume:/data" "$image" cat /data/result)" = volume-ok
echo "Nested Podman smoke passed: build, ownership, run/exec, network, port, bind, volume"
