#!/bin/bash
set -ev

mkdir -p dist
docker run --rm \
    -v $(pwd):/opt \
    -w /opt/ \
    -t zebralucky/electrum-dash-winebuild:Linux40x \
    /opt/contrib/build-linux/sdist/build.sh


sudo find . -name '*.po' -delete
sudo find . -name '*.pot' -delete


docker run --rm \
    -v $(pwd):/opt \
    -w /opt/contrib/build-linux/appimage \
    -t zebralucky/electrum-dash-winebuild:AppImage40x ./build.sh


BUILD_DIR=/root/build
TOR_PROXY_VERSION=0.4.2.6
TOR_PROXY_PATH=https://github.com/zebra-lucky/tor-proxy/releases/download
TOR_DIST=dist/tor-proxy-setup.exe

TOR_FILE=${TOR_PROXY_VERSION}/tor-proxy-${TOR_PROXY_VERSION}-win32-setup.exe
wget -O ${TOR_DIST} ${TOR_PROXY_PATH}/${TOR_FILE}
TOR_SHA=243d364015d340142b6a6d701c6509261e4574fa3009ba5febbe3982f25e7b46
echo "$TOR_SHA  $TOR_DIST" > sha256.txt
shasum -a256 -s -c sha256.txt


export WINEARCH=win32
export WINEPREFIX=/root/.wine-32
export PYHOME=$WINEPREFIX/drive_c/Python37


ZBARW_PATH=https://github.com/zebra-lucky/zbarw/releases/download/20180620
ZBARW_FILE=zbarw-zbarcam-0.10-win32.zip
ZBARW_SHA=eed1af99d68a1f9eab975843071bf088735cb79bf3188d511d06a3f1b4e10243
wget ${ZBARW_PATH}/${ZBARW_FILE}
echo "$ZBARW_SHA  $ZBARW_FILE" > sha256.txt
shasum -a256 -s -c sha256.txt
unzip ${ZBARW_FILE} && rm ${ZBARW_FILE} sha256.txt

X11_HASH_PATH=https://github.com/zebra-lucky/x11_hash/releases/download/1.4.1
X11_HASH_FILE=x11_hash-1.4.1-win32.zip
X11_HASH_SHA=66e7a97fc4afd8b0b95c771dd1f03d216cd5ca315dc3966714b8afe093405507
wget ${X11_HASH_PATH}/${X11_HASH_FILE}
echo "$X11_HASH_SHA  $X11_HASH_FILE" > sha256.txt
shasum -a256 -s -c sha256.txt
unzip ${X11_HASH_FILE} && rm ${X11_HASH_FILE} sha256.txt

LSECP256K1_PATH=https://github.com/zebra-lucky/secp256k1/releases/download/0.1
LSECP256K1_FILE=libsecp256k1-0.1-win32.zip
LSECP256K1_SHA=f750bbda859309f7dd7ab09e678995fead58654423e418ede9a46acc3abfcc66
wget ${LSECP256K1_PATH}/${LSECP256K1_FILE}
echo "$LSECP256K1_SHA  $LSECP256K1_FILE" > sha256.txt
shasum -a256 -s -c sha256.txt
unzip ${LSECP256K1_FILE} && rm ${LSECP256K1_FILE} sha256.txt


docker run --rm \
    -e WINEARCH=$WINEARCH \
    -e WINEPREFIX=$WINEPREFIX \
    -e PYHOME=$PYHOME \
    -e BUILD_DIR=$BUILD_DIR \
    -v $(pwd):$BUILD_DIR \
    -v $(pwd):$WINEPREFIX/drive_c/electrum-dash \
    -w $BUILD_DIR \
    -t zebralucky/electrum-dash-winebuild:Wine40x \
    $BUILD_DIR/contrib/build-wine/build.sh


export WINEARCH=win64
export WINEPREFIX=/root/.wine-64
export PYHOME=$WINEPREFIX/drive_c/Python37


ZBARW_FILE=zbarw-zbarcam-0.10-win64.zip
ZBARW_SHA=7705dfd9a1c4b9d07c9ae11502dbe2dc305d08c884f0825b35d21b312316e162
wget ${ZBARW_PATH}/${ZBARW_FILE}
echo "$ZBARW_SHA  $ZBARW_FILE" > sha256.txt
shasum -a256 -s -c sha256.txt
unzip ${ZBARW_FILE} && rm ${ZBARW_FILE} sha256.txt

X11_HASH_FILE=x11_hash-1.4.1-win64.zip
X11_HASH_SHA=1b2b6e9ba41c9b090910f8f480ed372f16c09d4bdc590f086b3cae2d7a23946e
wget ${X11_HASH_PATH}/${X11_HASH_FILE}
echo "$X11_HASH_SHA  $X11_HASH_FILE" > sha256.txt
shasum -a256 -s -c sha256.txt
unzip ${X11_HASH_FILE} && rm ${X11_HASH_FILE} sha256.txt

LSECP256K1_FILE=libsecp256k1-0.1-win64.zip
LSECP256K1_SHA=a7b4e39c69bfc363edd2d86acb88c4b9e30b7d1c0c3281a0985a10c8ce3e2cc0
wget ${LSECP256K1_PATH}/${LSECP256K1_FILE}
echo "$LSECP256K1_SHA  $LSECP256K1_FILE" > sha256.txt
shasum -a256 -s -c sha256.txt
unzip ${LSECP256K1_FILE} && rm ${LSECP256K1_FILE} sha256.txt

rm ${TOR_DIST}
TOR_FILE=${TOR_PROXY_VERSION}/tor-proxy-${TOR_PROXY_VERSION}-win64-setup.exe
wget -O ${TOR_DIST} ${TOR_PROXY_PATH}/${TOR_FILE}
TOR_SHA=0fcd79d167f866f570e6714974309b95d82f87fe872aca86067a52e811fb9421
echo "$TOR_SHA  $TOR_DIST" > sha256.txt
shasum -a256 -s -c sha256.txt
rm sha256.txt


docker run --rm \
    -e WINEARCH=$WINEARCH \
    -e WINEPREFIX=$WINEPREFIX \
    -e PYHOME=$PYHOME \
    -e BUILD_DIR=$BUILD_DIR \
    -v $(pwd):$BUILD_DIR \
    -v $(pwd):$WINEPREFIX/drive_c/electrum-dash \
    -w $BUILD_DIR \
    -t zebralucky/electrum-dash-winebuild:Wine40x \
    $BUILD_DIR/contrib/build-wine/build.sh
