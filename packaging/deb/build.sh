#!/bin/sh
# Builds the .deb for the architecture of the container.
# Bundles its own Python, so the package does not depend on the distribution's
# Python version or on packages Debian ships too old (pyVmomi, paho-mqtt).
#
#   docker run --rm -v "$PWD:/src:ro" -v "$PWD/dist:/out" python:3.14-slim sh /src/packaging/deb/build.sh
set -eu

SRC=/src
OUT=/out
NAME=ha-hypervisor-agent
PREFIX=/opt/$NAME
PYTHON_VERSION=3.14

VERSION=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$SRC/agent/ha_hypervisor_agent/__init__.py")
ARCH=$(dpkg --print-architecture)
ROOT=$(mktemp -d)/$NAME
WORK=$(mktemp -d)

pip install --quiet --no-cache-dir --root-user-action=ignore uv
export UV_PYTHON_INSTALL_DIR="$WORK/python"
uv python install --quiet "$PYTHON_VERSION"
# uv links the minor version to the full one: copy the real directory, not the link.
MANAGED=$(readlink -f "$(dirname "$(dirname "$(uv python find --managed-python "$PYTHON_VERSION")")")")

# Installed at the final path: entry point scripts keep the absolute interpreter path.
mkdir -p "$PREFIX"
cp -a "$MANAGED" "$PREFIX/python"
cp -a "$SRC/agent" "$WORK/agent"
uv pip install --quiet --no-cache --break-system-packages \
    --python "$PREFIX/python/bin/python3" "$WORK/agent"

LIB=$PREFIX/python/lib/python$PYTHON_VERSION
rm -rf "$LIB/test" "$LIB/idlelib" "$LIB/tkinter" "$LIB/turtledemo" "$LIB/ensurepip" \
    "$LIB"/config-* "$PREFIX/python/include" "$PREFIX/python/share" \
    "$PREFIX/python/lib/pkgconfig" "$PREFIX"/python/lib/libtcl* "$PREFIX"/python/lib/libtk* \
    "$PREFIX"/python/lib/tcl* "$PREFIX"/python/lib/tk* "$PREFIX"/python/lib/itcl* \
    "$PREFIX"/python/lib/thread*
find "$PREFIX" -type d -name __pycache__ -prune -exec rm -rf {} +
"$PREFIX/python/bin/python3" -m compileall -q -j 0 --invalidation-mode unchecked-hash "$LIB"

mkdir -p "$ROOT/opt" "$ROOT/usr/bin" "$ROOT/usr/lib/systemd/system" "$ROOT/usr/share/doc/$NAME" "$ROOT/DEBIAN"
cp -a "$PREFIX" "$ROOT/opt/"
ln -s "$PREFIX/python/bin/$NAME" "$ROOT/usr/bin/$NAME"
install -m 0644 "$SRC/packaging/deb/$NAME.service" "$ROOT/usr/lib/systemd/system/$NAME.service"
install -m 0644 "$SRC/packaging/deb/$NAME@.service" "$ROOT/usr/lib/systemd/system/$NAME@.service"
install -m 0644 "$SRC/packaging/deb/copyright" "$ROOT/usr/share/doc/$NAME/copyright"
install -m 0644 "$SRC/agent/config.example.yaml" "$ROOT/usr/share/doc/$NAME/config.example.yaml"
for script in postinst prerm postrm; do
    install -m 0755 "$SRC/packaging/deb/$script" "$ROOT/DEBIAN/$script"
done

SIZE=$(du -sk "$ROOT/opt" "$ROOT/usr" | awk '{sum += $1} END {print sum}')
sed -e "s/@VERSION@/$VERSION/" -e "s/@ARCH@/$ARCH/" -e "s/@SIZE@/$SIZE/" \
    "$SRC/packaging/deb/control.in" > "$ROOT/DEBIAN/control"
chmod 0644 "$ROOT/DEBIAN/control"

FILE=${NAME}_${VERSION}_${ARCH}.deb
dpkg-deb --root-owner-group -Zxz --build "$ROOT" "$OUT/$FILE"
(cd "$OUT" && sha256sum "$FILE" > "$FILE.sha256")
ls -l "$OUT/$FILE"
