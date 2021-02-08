#!/bin/bash
set -ev

./contrib/make_locale
find . -name '*.po' -delete
find . -name '*.pot' -delete

# patch buildozer to support APK_VERSION_CODE env
VERCODE_PATCH_PATH=/home/buildozer/build/contrib/dash/travis
VERCODE_PATCH="$VERCODE_PATCH_PATH/read_apk_version_code.patch"

DOCKER_CMD="pushd /opt/buildozer"
DOCKER_CMD="$DOCKER_CMD && patch -p0 < $VERCODE_PATCH && popd"
DOCKER_CMD="$DOCKER_CMD && rm -rf packages"
DOCKER_CMD="$DOCKER_CMD && ./contrib/make_packages"
DOCKER_CMD="$DOCKER_CMD && rm -rf packages/bls_py"
DOCKER_CMD="$DOCKER_CMD && rm -rf packages/python_bls*"
DOCKER_CMD="$DOCKER_CMD && ./contrib/android/make_apk"

if [[ $ELECTRUM_MAINNET == "false" ]]; then
    DOCKER_CMD="$DOCKER_CMD release-testnet"
fi

sudo chown -R 1000 .
docker run --rm \
    --env APP_ANDROID_ARCH=$APP_ANDROID_ARCH \
    --env APK_VERSION_CODE=$DASH_ELECTRUM_VERSION_CODE \
    -v $(pwd):/home/buildozer/build \
    -t zebralucky/electrum-dash-winebuild:Kivy40x bash -c \
    "$DOCKER_CMD"

FNAME_TAIL=release-unsigned.apk
if [[ $ELECTRUM_MAINNET == "false" ]]; then
  PATHNAME_START=bin/Electrum_DASH_Testnet
else
  PATHNAME_START=bin/Electrum_DASH
fi
