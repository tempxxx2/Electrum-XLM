#!/bin/bash

source ./contrib/dash/travis/electrum_dash_version_env.sh;
echo wine build version is $DASH_ELECTRUM_VERSION

mv $BUILD_DIR/zbarw $WINEPREFIX/drive_c/

mv $BUILD_DIR/x11_hash $WINEPREFIX/drive_c/

mv $BUILD_DIR/libsecp256k1 $WINEPREFIX/drive_c/

cd $WINEPREFIX/drive_c/electrum-dash

rm -rf build
rm -rf dist/electrum-dash

cp contrib/build-wine/deterministic.spec .
cp contrib/dash/pyi_runtimehook.py .
cp contrib/dash/pyi_tctl_runtimehook.py .

wine python -m pip install --no-warn-script-location dist/pyinstaller*.whl
rm dist/pyinstaller*.whl

wine python -m pip install --no-dependencies --no-warn-script-location \
    -r contrib/deterministic-build/requirements.txt
wine python -m pip install --no-dependencies --no-warn-script-location \
    -r contrib/deterministic-build/requirements-hw.txt
wine python -m pip install --no-dependencies --no-warn-script-location \
    -r contrib/deterministic-build/requirements-binaries.txt
wine python -m pip install --no-dependencies --no-warn-script-location \
    -r contrib/deterministic-build/requirements-build-wine.txt

wine pyinstaller --clean -y \
    --name electrum-dash-$DASH_ELECTRUM_VERSION.exe \
    deterministic.spec

if [[ $WINEARCH == win32 ]]; then
    NSIS_EXE="$WINEPREFIX/drive_c/Program Files/NSIS/makensis.exe"
else
    NSIS_EXE="$WINEPREFIX/drive_c/Program Files (x86)/NSIS/makensis.exe"
fi

wine "$NSIS_EXE" /NOCD -V3 \
    /DPRODUCT_VERSION=$DASH_ELECTRUM_VERSION \
    /DWINEARCH=$WINEARCH \
    contrib/build-wine/electrum-dash.nsi
