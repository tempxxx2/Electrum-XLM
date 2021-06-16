#!/bin/bash

set -ev

export PYINSTALLER_REPO="https://github.com/SomberNight/pyinstaller.git"
export PYINSTALLER_COMMIT="80ee4d613ecf75a1226b960a560ee01459e65ddb"
# ^ tag 4.2, plus a custom commit that fixes cross-compilation with MinGW

if [ "$WINEARCH" = "win32" ] ; then
    export PYINST_ARCH="32bit"
elif [ "$WINEARCH" = "win64" ] ; then
    export PYINST_ARCH="64bit"
else
    fail "unexpected WINEARCH: $WINEARCH"
fi

# we build our own PyInstaller boot loader as the default one has high
# anti-virus false positives
echo "Building PyInstaller bootloaders for $WINEARCH."

cd "$WINEPREFIX/drive_c/electrum-dash"
rm -rf dist/pyinstaller/
mkdir -p dist/pyinstaller/
cd dist/pyinstaller
# Shallow clone
git init
git remote add origin $PYINSTALLER_REPO
git fetch --depth 1 origin $PYINSTALLER_COMMIT
git checkout -b pinned "${PYINSTALLER_COMMIT}^{commit}"
rm -fv PyInstaller/bootloader/Windows-*/run*.exe || true

# add reproducible randomness. this ensures we build a different bootloader for each commit.
# if we built the same one for all releases, that might also get anti-virus false positives
echo -e "\nconst char *dash_electrum_tag" \
        " = \"tagged by Dash-Electrum@$ELECTRUM_COMMIT_HASH\";" \
        >> ./bootloader/src/pyi_main.c
cd bootloader
# cross-compile to Windows using host python
python3 ./waf all --target-arch=${PYINST_ARCH} \
    CC="${GCC_TRIPLET_HOST}-gcc" \
    AR="${GCC_TRIPLET_HOST}-ar" \
    CFLAGS="-static \
            -Wno-dangling-else \
            -Wno-error=unused-value \
            -Wno-error=implicit-function-declaration \
            -Wno-error=int-to-pointer-cast"
