#!/bin/sh
# Construye dist/xampp-linux_<versión>_all.deb   Uso: ./build-deb.sh [versión]
set -e
cd "$(dirname "$0")"
VERSION="${1:-$(sed -n 's/^Version: //p' packaging/DEBIAN/control)}"
STAGE="build/xampp-linux_${VERSION}_all"
rm -rf build && mkdir -p "$STAGE" dist
cp -r packaging/. "$STAGE/"
sed -i "s/^Version: .*/Version: ${VERSION}/" "$STAGE/DEBIAN/control"
mkdir -p "$STAGE/usr/lib/xampp-linux" "$STAGE/usr/share/icons/hicolor/scalable/apps"
cp src/xampp_linux.py src/xampp-linux.svg "$STAGE/usr/lib/xampp-linux/"
cp src/xampp-linux.svg "$STAGE/usr/share/icons/hicolor/scalable/apps/xampp-control-panel.svg"
printf 'xampp-linux (%s) unstable; urgency=low\n\n  * Release %s.\n\n -- %s  %s\n' \
    "$VERSION" "$VERSION" "$(sed -n 's/^Maintainer: //p' packaging/DEBIAN/control)" "$(date -R)" \
    | gzip -9n > "$STAGE/usr/share/doc/xampp-linux/changelog.gz"
chmod 755 "$STAGE/DEBIAN/postinst" "$STAGE/DEBIAN/postrm" "$STAGE/usr/bin/xampp-control-panel"
chmod 644 "$STAGE"/usr/lib/xampp-linux/*
dpkg-deb --root-owner-group --build "$STAGE" "dist/xampp-linux_${VERSION}_all.deb" >/dev/null
echo "Listo: dist/xampp-linux_${VERSION}_all.deb"
