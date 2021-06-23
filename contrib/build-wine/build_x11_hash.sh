#!/bin/bash

set -ev

export X11_HASH_REPO="https://github.com/zebra-lucky/x11_hash.git"
export X11_HASH_COMMIT="ecdf417847601ae74a3ed1a2b787c80a22264a3d"

echo "Building x11_hash for $WINEARCH."
export PROJ_ROOT=$WINEPREFIX/drive_c/electrum-dash
export DIST_DIR=$WINEPREFIX/drive_c/x11_hash

cd $PROJ_ROOT
rm -rf dist/x11_hash/
mkdir -p dist/x11_hash/
cd dist/x11_hash
export PREFIX_DIR="$(pwd)/dist"
# Shallow clone
git init
git remote add origin $X11_HASH_REPO
git fetch --depth 1 origin $X11_HASH_COMMIT
git checkout -b pinned "${X11_HASH_COMMIT}^{commit}"

# add reproducible randomness.
echo -e "\nconst char *dash_electrum_tag" \
        " = \"tagged by Dash-Electrum@$ELECTRUM_COMMIT_HASH\";" \
        >> ./x11hash.c

autoreconf -fi || fail "Could not run autoreconf."
./configure --host=${GCC_TRIPLET_HOST} --prefix="${PREFIX_DIR}"
make -j4 || fail "Could not build."
make install || fail "Could not install."
. "${PREFIX_DIR}/lib/libx11hash.la"
$host_strip "${PREFIX_DIR}/lib/$dlname"
mkdir -p $DIST_DIR
cp -fpv "${PREFIX_DIR}/lib/$dlname" $DIST_DIR || fail "Could not copy."
