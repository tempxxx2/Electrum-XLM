#!/bin/bash
set -ev

if [[ $ELECTRUM_MAINNET == "true" ]] && [[ -z $IS_RELEASE ]]; then
    # do not build mainnet apk if is not release
    exit 0
fi

cd build
if [[ -n $TRAVIS_TAG ]]; then
    BUILD_REPO_URL=https://github.com/akhavr/electrum-dash.git
    git clone --branch $TRAVIS_TAG $BUILD_REPO_URL electrum-dash
else
    git clone .. electrum-dash
fi


pushd electrum-dash
./contrib/make_locale
find . -name '*.po' -delete
find . -name '*.pot' -delete
popd

# patch buildozer to support APK_VERSION_CODE env
VERCODE_PATCH_PATH=/home/buildozer/build/contrib/dash/travis
VERCODE_PATCH="$VERCODE_PATCH_PATH/read_apk_version_code.patch"

DOCKER_CMD="pushd /opt/buildozer"
# commit: from branch sombernight/202104_android_adaptiveicon
DOCKER_CMD="$DOCKER_CMD && git fetch --all"
DOCKER_CMD="$DOCKER_CMD && git checkout 'c17ac3618334c9936253e8f5b88dce43dc4da75b^{commit}'"
DOCKER_CMD="$DOCKER_CMD && patch -p0 < $VERCODE_PATCH && popd"
DOCKER_CMD="$DOCKER_CMD && pushd /opt/python-for-android"
DOCKER_CMD="$DOCKER_CMD && git fetch --all"
# commit: android: add support for adaptive icon/launcher
DOCKER_CMD="$DOCKER_CMD && git checkout '5356bc7838b03c8c174c91fe01539c91d1b40b9f^{commit}'"
DOCKER_CMD="$DOCKER_CMD && git revert --no-edit '257cfacbdd523af0b5b6bb5b2ba64ab7a5c82d58'"
DOCKER_CMD="$DOCKER_CMD && popd"
DOCKER_CMD="$DOCKER_CMD && rm -rf packages"
DOCKER_CMD="$DOCKER_CMD && ./contrib/make_packages"
DOCKER_CMD="$DOCKER_CMD && rm -rf packages/bls_py"
DOCKER_CMD="$DOCKER_CMD && rm -rf packages/python_bls*"
DOCKER_CMD="$DOCKER_CMD && ./contrib/android/make_apk"

if [[ $ELECTRUM_MAINNET == "false" ]]; then
    DOCKER_CMD="$DOCKER_CMD release-testnet"
fi

sudo chown -R 1000 electrum-dash
docker run --rm \
    --env APP_ANDROID_ARCH=$APP_ANDROID_ARCH \
    --env APK_VERSION_CODE=$DASH_ELECTRUM_VERSION_CODE \
    -v $(pwd)/electrum-dash:/home/buildozer/build \
    -t zebralucky/electrum-dash-winebuild:Kivy40x bash -c \
    "$DOCKER_CMD"
