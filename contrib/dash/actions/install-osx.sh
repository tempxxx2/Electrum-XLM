#!/bin/bash
set -ev

if [[ -n $GITHUB_ACTION ]]; then
    PYTHON_VERSION=3.7.6
    PYFTP=https://www.python.org/ftp/python/$PYTHON_VERSION
    PYPKG_NAME=python-$PYTHON_VERSION-macosx10.6.pkg
    PY_SHA256=1a595e7e6aca247b8229bd00a8e5fc8879fd85e18a2aa747ee7ecffb500cbfdd
    echo "$PY_SHA256  $PYPKG_NAME" > $PYPKG_NAME.sha256
    curl -O $PYFTP/$PYPKG_NAME
    shasum -a256 -s -c $PYPKG_NAME.sha256
    sudo installer -pkg $PYPKG_NAME -target /
    rm $PYPKG_NAME $PYPKG_NAME.sha256
fi

LIBUSB_VERSION=1.0.23
LIBUSB_URI=https://dl.bintray.com/homebrew/bottles/
LIBUSB_FILE=libusb-${LIBUSB_VERSION}.sierra.bottle.tar.gz
LIBUSB_SHA256=600db569dd82dda3e492e53c8023093d003836329b614cea8064ab68d20aca0d
echo "$LIBUSB_SHA256  $LIBUSB_FILE" > $LIBUSB_FILE.sha256
curl -O ${LIBUSB_URI}${LIBUSB_FILE}
shasum -a256 -s -c $LIBUSB_FILE.sha256
tar -xzf ${LIBUSB_FILE}
cp libusb/${LIBUSB_VERSION}/lib/libusb-1.*.dylib .
rm -rf libusb/ ${LIBUSB_FILE} ${LIBUSB_FILE}.sha256

LIBGMP_VERSION=6.1.2_2
LIBGMP_BOTTLE_VER=.1
LIBGMP_URI=https://dl.bintray.com/homebrew/bottles/
LIBGMP_FILE=gmp-${LIBGMP_VERSION}.sierra.bottle${LIBGMP_BOTTLE_VER}.tar.gz
LIBGMP_SHA256=ada22a8bbfe8532d71f2b565e00b1643beaf72bff6b36064cbad0cd7436e4948
echo "$LIBGMP_SHA256  $LIBGMP_FILE" > $LIBGMP_FILE.sha256
wget -O ${LIBGMP_FILE} ${LIBGMP_URI}${LIBGMP_FILE}
shasum -a256 -s -c $LIBGMP_FILE.sha256
tar -xzf ${LIBGMP_FILE}
cp gmp/${LIBGMP_VERSION}/lib/libgmp.10.dylib .
rm -rf gmp/ ${LIBGMP_FILE} ${LIBGMP_FILE}.sha256

LSECP256K1_PATH=https://github.com/zebra-lucky/secp256k1/releases/download/0.1
LSECP256K1_FILE=libsecp256k1-0.1-osx.tgz
LIB_SHA256=4a064681a0e1a83a5692d88883978f025a8dda7772d1f963ac681b9e321b89bb
echo "$LIB_SHA256  $LSECP256K1_FILE" > $LSECP256K1_FILE.sha256
curl -O -L ${LSECP256K1_PATH}/${LSECP256K1_FILE}
shasum -a256 -s -c ${LSECP256K1_FILE}.sha256
tar -xzf ${LSECP256K1_FILE}
cp libsecp256k1/libsecp256k1.0.dylib .
rm -rf libsecp256k1/ ${LSECP256K1_FILE} ${LSECP256K1_FILE}.sha256
