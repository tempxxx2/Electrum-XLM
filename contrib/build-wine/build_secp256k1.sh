#!/bin/bash

set -ev

export SECP256K1_REPO="https://github.com/bitcoin-core/secp256k1.git"
export SECP256K1_COMMIT="dbd41db16a0e91b2566820898a3ab2d7dad4fe00"

echo "Building secp256k1 for $WINEARCH."
export PROJ_ROOT=$WINEPREFIX/drive_c/electrum-dash
export DIST_DIR=$WINEPREFIX/drive_c/libsecp256k1

cd $PROJ_ROOT
rm -rf dist/secp256k1/
mkdir -p dist/secp256k1/
cd dist/secp256k1
export PREFIX_DIR="$(pwd)/dist"
# Shallow clone
git init
git remote add origin $SECP256K1_REPO
git fetch --depth 1 origin $SECP256K1_COMMIT
git checkout -b pinned "${SECP256K1_COMMIT}^{commit}"

# add reproducible randomness.
export release_tag="\nconst char *dash_electrum_tag"
export release_tag=${release_tag}" = \"tagged by Dash-Electrum@"
export release_tag=${release_tag}"$ELECTRUM_COMMIT_HASH\";"
echo -e ${release_tag} >> ./src/secp256k1.c
echo -e ${release_tag} >> ./src/gen_context.c

echo "libsecp256k1_la_LDFLAGS = -no-undefined" >> Makefile.am
echo "LDFLAGS = -no-undefined" >> Makefile.am
./autogen.sh || fail "Could not run autogen."
./configure \
    --host=${GCC_TRIPLET_HOST} \
    --prefix="${PREFIX_DIR}" \
    --enable-module-recovery \
    --enable-experimental \
    --enable-module-ecdh \
    --disable-benchmark \
    --disable-tests \
    --disable-exhaustive-tests \
    --disable-static \
    --enable-shared || fail "Could not configure."
make -j4 || fail "Could not build."
make install || fail "Could not install."
. "${PREFIX_DIR}/lib/libsecp256k1.la"
$host_strip "${PREFIX_DIR}/lib/$dlname"
mkdir -p $DIST_DIR
cp -fpv "${PREFIX_DIR}/lib/$dlname" $DIST_DIR || fail "Could not copy."
